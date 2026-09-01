# Architecture

## The recovery loop (5 steps)

1. **Detect** - consume a stream of at-risk events (failed one-time payments, halted subscriptions).
2. **Diagnose** - map each raw failure reason to one canonical root cause.
3. **Decide** - a deterministic policy picks the intervention that fits the cause.
4. **Act, within bounds** - execute via Razorpay test APIs, check whether the money
   arrived, obey hard caps and stopping rules, log every step.
5. **Measure** - over the whole batch: rupees recovered, % recovered, avg attempts,
   count escalated, rule violations (target 0), double charges (target 0). Broken down
   by root cause, compared against naive baselines.

## Components

```
                 config/policy.yaml      dunning/config.py
                  (cause -> steps)   (limits, vocab, secrets, paths)
                         |                     |
generate.py --> feed.py --> [ diagnose.py --> policy.py --> execute.py ] --> audit.py
 cases.jsonl   events.jsonl    |         |          |           |            audit.jsonl
 (+ latent,   (HMAC-signed     |    Diagnosis    Plan     Clock + RazorpayGateway   + manifest
  showcase)    webhooks)       |                          + GuardrailLedger          (hash chain)
                               |                          + SpendGovernor
                          llm.py (Gemini,               guardrails.py
                          classify only)              messaging.py (per-channel nudge)
                               |
                       run_batch.py  ---- baseline.py (naive_one_retry, blind_three)
                               |
                          report.py  -->  reports/latest.html
```

| component | file | role |
|---|---|---|
| generator | `generate.py` | ~300 synthetic at-risk cases with a hidden `latent` recovery model; plants + tags the showcase edge cases |
| event feed | `feed.py` | each case → an HMAC-signed, Razorpay-webhook-shaped event, time-ordered; `verify_signature` on ingest |
| diagnoser | `diagnose.py` | event → one of 15 root causes via a cheapest-first cascade; LLM only for messy free text |
| LLM wrapper | `llm.py` | the one place Gemini is called: `classify_failure` + generic `complete`; provider select, response cache, offline fallback |
| policy engine | `policy.py` + `config/policy.yaml` | root cause + context → a fixed, bounded `Plan` (interventions + schedule) |
| executor | `execute.py` | walks a plan against real Razorpay test APIs; virtual `Clock`; `RazorpayGateway` (idempotent); rolls `latent` for the pay/no-pay outcome; one re-plan on a cause shift; status check before every charge |
| guardrails | `guardrails.py` | `GuardrailLedger` (allow/defer/skip/halt per step), `SpendGovernor` (run ceiling + circuit breaker), independent violation recompute |
| message writer | `messaging.py` | per-channel (SMS / WhatsApp), per-language customer nudge; deterministic safety validator + template fallback |
| redaction | `redact.py` | mask phone/email, first name only, strip secret-shaped tokens - applied to everything logged |
| audit log | `audit.py` | append-only hash-chained `logs/audit.jsonl` + signed manifest; `verify()` / `make verify-audit` |
| batch runner | `run_batch.py` | the whole pipeline in one call + the baselines; produces a `RunResult` |
| report | `report.py` | one self-contained `reports/latest.html` |
| baselines | `baseline.py` | naive strategies sharing the executor's outcome oracle and seed |

### Synthetic data (`dunning/generate.py`, step 2)

There is no real customer paying in Razorpay test mode, so each generated case
hides a `latent` block: probabilities and one salary day that the **executor**
(step 6) rolls a *seeded* RNG against at each attempt to decide whether the money
actually arrives. Outcomes are therefore probabilistic and emergent but fully
reproducible - the same seed always yields the same batch and the same recoveries.
Per-case RNG is seeded by `sha256(seed : case_id)`, so a case is independent of
batch size and generation order.

Baked-in patterns a disciplined agent can exploit (and a naive one misses):
`insufficient_funds` recovers far more often when retried within ~1 day of
`funds_return_day`; `expired_card` never clears on retry (needs switch-method /
payment link); `bank_timeout` is usually transient (one quick retry often works);
`mandate_cancelled` must never be auto-retried; `abandoned` has nothing to retry.
A `chronic` slice is unrecoverable by design so "% recovered" stays honest, and a
small subset of subscriptions has `mandate_revokes_at_attempt` set - the mandate
dies mid-sequence (step 12's deliberate failure).

Time is a **virtual clock**: the batch runner keeps a simulated "now" it
fast-forwards between attempts, so the >=24h-gap rule, the 09:00-20:00 contact
window, and the salary-day timing pattern genuinely function without waiting in
real time.

### Event feed (`dunning/feed.py`, step 3)

In production the agent is triggered by a **Razorpay webhook** - an HTTP POST
Razorpay sends us when a payment fails or a subscription charge bounces. We can't
receive those (no public server, no real failing payments in test mode), so the
brief's instruction is to replay them from a file. `feed.py` turns each case into
an event with the same envelope a real webhook delivery has
(`entity: "event"`, `account_id`, `event`, `contains`, `payload.<entity>.entity`,
unix `created_at`), sorts them by failure time, and writes `data/events.jsonl`.
`replay()` yields them one at a time in order.

Event types: `payment.failed` and `subscription.pending` are real Razorpay
events; `order.abandoned` is our own stand-in (Razorpay fires nothing for a pure
abandonment - in production this would come from our own "order stuck in
created" monitor). The case is linked back to its event through
`notes.case_id` (Razorpay `notes` is a real free-form field). The hidden
`latent` block is never copied into an event: the agent sees only what a webhook
would carry; `latent` stays with the executor.

### Diagnoser (`dunning/diagnose.py`, step 4)

`diagnose(event) -> Diagnosis` maps one event to one canonical root cause via a
cheapest-first cascade:

| stage | source | AI? | share of batch |
|---|---|---|---|
| `event` | an `order.abandoned` event *is* the diagnosis | no | ~6% |
| `error_reason` | Razorpay's structured code (`insufficient_funds`, `card_expired`, `gateway_technical_error`, `payment_mandate_revoked`, …) via a lookup table | no | ~78% |
| `text_rules` | a small, deliberately *literal* regex table over `error_description` ("expired", "timeout", "insuff bal", "504", "mandate", …) | no | ~7% |
| `llm` | `llm.classify_failure` — only free text stages above couldn't place | **yes** | ~9% |

The `Diagnosis` records the label, a confidence, the stage, and the exact signal
it matched, so every classification is auditable. An LLM label below 0.4
confidence, or `unknown`, is recorded as `unknown` (the policy engine escalates
those to a human rather than guessing).

The text-rules table is kept intentionally conservative — literal tokens only.
Anything that needs interpretation ("the paycheck is late this month", "issuer
bank was flaky") is left for the LLM on purpose; that split is the whole point of
having both.

### LLM wrapper (`dunning/llm.py`)

The single module that calls an LLM. Two jobs: `classify_failure()` (step 4) and
`write_message()` (step 8, provisional). Provider is `LLM_PROVIDER` (default
Gemini); with no `GEMINI_API_KEY` it falls back to a deterministic offline
heuristic so the pipeline and tests run with zero setup. Every real answer is
written to `data/llm_cache.json` keyed by a hash of the exact input, so re-running
the batch makes no API calls and is byte-for-byte reproducible. A failed API call
returns `unknown` rather than raising — one flaky call must never break a batch.

### Policy engine (`dunning/policy.py` + `config/policy.yaml`, step 5)

`plan(root_cause, context) -> Plan` is a pure function. It produces a **fixed**
ordered list of `Step`s (each an intervention + a minimum `Wait`), decided up
front so the whole plan is auditable and testable before any action runs.

The 15 causes map to one of eight **templates** in `config/policy.yaml` — the
decision spine, readable without opening any code:

| template | shape | causes |
|---|---|---|
| `transient` | retry_now → retry_later → human | bank_timeout, technical_decline |
| `slow_transient` | retry_later(8h) → retry_later → human | issuer_unavailable |
| `timing_problem` | reminder → retry at month-start → retry → human | insufficient_funds |
| `one_retry_then_link` | retry_later → payment_link → human | do_not_honour, card_limit_exceeded |
| `link_first` | payment_link → payment_link → human | expired_card, three_ds_failed, invalid_payment_details, international_blocked, card_declined_risk, abandoned |
| `mandate_repair` | mandate_link → mandate_link → human | mandate_cancelled |
| `do_not_reengage` | human only | stolen_or_lost_card |
| `escalate` | human only | needs_review |

`policy.py` then applies the **context adjustments** that are branch logic, not a
table:

1. **Dead mandate** on a subscription → the whole plan becomes `mandate_repair`,
   whatever the diagnosed cause; the charge can't succeed until it's fixed, and
   auto-retrying a dead mandate breaches its terms.
2. **High-value risk block** (`card_declined_risk` ≥ ₹10,000) → straight to a
   human; no link, no retry.
3. **Unreachable customer** → drop reminder steps, turn link steps into a human
   handoff (retries stay - they don't need the customer).
4. **Low value** (< ₹150) → stop after the automated attempts; a manual touch
   costs more than the money at stake (`do_nothing`, recorded as written off).
5. **Guardrail clamp** — a safety net that guarantees ≤3 retries and ≤2 messages
   even if a future template slips past the caps.

Every plan ends in `handoff_human` or `do_nothing` — the agent never just stops.
`Plan.replan_allowed` is `False` for `stolen_or_lost_card`, `card_declined_risk`
and `needs_review` (and after a high-value escalation); for everything else the
executor may request **one** re-plan if a later attempt fails with a materially
different cause (step 12's mandate-dies-mid-sequence is exactly this).

`schedule(plan, t0)` resolves each `Wait` to an earliest datetime (including
"next month-start"); the ≥24h spacing and the 09:00–20:00 contact window are
layered on by the guardrails in step 7, not here.

### Executor (`dunning/execute.py`, step 6)

`execute_plan(case, plan, clock) -> ExecutionResult` walks a plan's scheduled
steps, one at a time, advancing a virtual `Clock` to each step's guarded time.

**Real vs simulated.** `retry_*` really calls `client.order.create`;
`send_*_link` really calls `client.payment_link.create`. Both go through
`RazorpayGateway`, which attaches an idempotency key (`dun_<case>_<i>_<action>`)
and returns the first response for a repeated key - a step can never create two
charges. With `RAZORPAY_DRY_RUN=1` or no keys, a local fake with the same shape
stands in (its ids are derived from the idempotency key, so runs stay
reproducible). The **only** simulated thing is whether the customer actually
paid: `_recovered()` rolls the case's hidden `latent` with a seeded RNG
(`f"{seed}:{case_id}:outcome"`). The agent never sees `latent`.

**Guardrail timing** (minimal here; step 7 formalises violation tracking): a
retry is pushed out to ≥24h after the previous retry; a message is moved into
the 09:00-20:00 window; a hard cap of 5 actions forces a handoff.

**Stopping rules** end the sequence immediately: money recovered; the customer
replies stop / unsubscribe / dispute; the subscription mandate is found dead;
retries or the action cap are exhausted; escalation to a human. Every result
carries `stop_reason` (a `config.STOP_REASONS` value) and a full per-attempt
log (action, scheduled vs actual time, Razorpay ref, idempotency key, outcome).

**Re-planning.** `_cause_shift()` reports a materially different cause revealed
mid-run. Today the simulator only models the mandate dying on the Nth action
(`latent.mandate_revokes_at_attempt`); when it fires and `plan.replan_allowed`,
the executor re-plans **once** to mandate repair and continues from the current
clock time. This is step 12's deliberate failure, and it already works: retries
stop, the mandate link is tried, then a human handoff - 0 rule violations.

### Guardrail layer (`dunning/guardrails.py`, step 7)

Every hard limit lives here and is applied identically to every case.

`GuardrailLedger` (one per case). The executor calls `evaluate(action, when)`
*before* each step and acts on the `Decision`:

| kind | meaning | rules that produce it |
|---|---|---|
| `allow` | do it at the requested time | — |
| `defer` | do it, but later | `retry_spacing` (≥24h since last retry), `contact_window` (09:00–20:00) |
| `skip` | don't do this step, move on | `retry_cap` (3), `message_cap` (2) |
| `halt` | stop the sequence, escalate | `action_hard_cap` (5) |

Because the ledger decides *before* the action, a violation cannot happen. Every
`Decision` is retained, so the audit log shows why each step ran when it did — or
didn't. `record()` advances the counters only once the executor has actually
performed the step (a re-plan's aborted step never counts).

`SpendGovernor` (one per run). `may_attempt(paise)` gates auto-charges against a
run-wide ceiling; `note_gateway_error()` feeds a circuit breaker that stops the
batch launching new cases after `MONEY.max_gateway_errors`.

`assert_no_violations(results)` — an independent recomputation from the finished
attempt logs (counting only steps actually carried out). The ledger makes this
structurally 0; the batch asserts it every run.

### Security controls (cross-cutting)

- **PII minimisation** — `dunning/redact.py` masks phone/email and keeps only a
  first name; nothing with raw PII or a secret-shaped token reaches a log or the
  report.
- **Fail-safe gateway** — live Razorpay calls require `RAZORPAY_LIVE=1` *and*
  both keys *and* dry-run off. Any other state uses the local fake.
- **Webhook signatures** — every replayed event is HMAC-SHA256 signed
  (`WEBHOOK_SECRET`) and verified on ingest; unsigned / tampered events are
  quarantined.
- **Prompt-injection resistance** — the classifier prompt fences the failure text
  between markers, tells the model it is untrusted data, and never does anything
  with the output but map it to the fixed enum (low confidence → `needs_review`).
- **Money rails** — per-action amount band + a run-wide auto-charge ceiling,
  checked before any gateway call.
- **Idempotency that survives restarts** — in live mode the key→response map is
  persisted to disk, so re-running a batch cannot create a second charge.
- **Tamper-evident audit log** — `dunning/audit.py`. Every run writes
  `logs/audit.jsonl`: one line per fact (`run_start`, then per case a
  `diagnosis`, a `plan`, an `attempt` per attempt, a `case_summary`, then
  `run_end`), each carrying `prev_hash` and its own `hash` (sha256 of the line
  without its hash field). `logs/audit.manifest.json` pins the chain head, the
  record count, a guardrail/policy config fingerprint, and an HMAC of all of it
  under `AUDIT_SECRET`. `audit.verify()` (`make verify-audit`) recomputes the
  chain and checks the manifest — any edit, drop, reorder, or a config change
  since the run is caught. Customer data passes through `redact.redact_customer`
  and free text through `redact.sanitize`, so no raw PII is stored.

### Message writer (`dunning/messaging.py`, step 8)

The LLM's second and last job. `compose()` returns one `Message` per channel:

* **sms** - ≤160 chars, plain, no emoji
* **whatsapp** - 2-3 lines, conversational, one emoji

in the customer's language (`en`, `hi` Devanagari, `hinglish` Latin script).

Every candidate goes through `_validate()`: it must state the amount, carry the
link when one was supplied, stay within the length cap, and must **not** contain
threat / coercion words or a raw email / phone number. If it fails - or the
provider is `fake` (no key) - a fixed per-channel/per-language template is used,
and `Message.from_template` + `Message.issues` record why. So the pipeline always
emits a safe message, and the audit log keeps the exact text sent on each
channel.

No tone A/B: with no real recipients there is nothing to optimise a lift
against, so one polite, plain register is used everywhere.

## Where we deliberately did NOT use an LLM, and why

The retry schedule, every limit and stopping rule, all money math, the "did it
arrive?" check, the audit log, and every metric are plain deterministic code.
Money decisions must be reproducible and explainable line by line; an LLM is
non-deterministic and hard to audit. Even inside the diagnoser, ~91% of events
are classified by lookup tables and literal rules — the LLM only sees the ~9%
of genuinely messy free text, and even then it only *labels*: it never chooses
an action, a schedule, or an amount. The other LLM use is writing the customer
nudge message (step 8), where natural phrasing is the point.

## Guardrails, in one place

All defined in `dunning/config.py`:

| limit | value | enforced by |
|---|---|---|
| retries per case | ≤ 3 | `GuardrailLedger` → `skip` |
| gap between retries | ≥ 24 h | `GuardrailLedger` → `defer` |
| contact window | 09:00–20:00 local | `GuardrailLedger` → `defer` |
| messages per customer | ≤ 2 | `GuardrailLedger` → `skip` |
| actions per case (any kind) | ≤ 5 | `GuardrailLedger` → `halt` |
| single auto-charge | ₹1 – ₹1,00,000 | `execute._amount_ok` → block + escalate |
| auto-charge per run | ≤ ₹2 crore | `SpendGovernor.may_attempt` |
| gateway errors per run | ≤ 15 then abort | `SpendGovernor.tripped` |

**Stopping rules** (the sequence ends immediately): money recovered; customer
reply matches `stop` / `unsubscribe` / `dispute`; subscription mandate found
dead; retries or the action cap exhausted; escalation to a human; deliberate
write-off. Each result carries one `config.STOP_REASONS` value.

### Batch runner + report (`dunning/run_batch.py`, `dunning/report.py`, step 10)

`run(seed, count)` does the whole pipeline in one call — generate → sign feed →
per event: verify signature → diagnose → plan → execute → audit — then runs the
naive baselines over the *same* cases and seed and hands a `RunResult` to
`report.write`, which renders one self-contained `reports/latest.html` (inline
CSS, no JS, no CDN). Single seed; deterministic; the seed is on the report.

## Metrics definitions

- **at-risk value** — sum of `amount_paise` over every case in the run.
- **recovered** — a case whose `ExecutionResult.recovered` is true, i.e. an
  attempt's simulated outcome (rolled from `latent`) came back as money in. Its
  full amount counts once; partial recovery is not modelled.
- **% of at-risk value** — recovered paise ÷ at-risk paise.
- **avg attempts** — mean length of the per-case attempt log (skipped / deferred
  rows included, since they are still decisions the agent made).
- **escalated** — `stop_reason == "escalated_to_human"`.
- **written off** — `stop_reason == "written_off"` (value below the cost of a
  manual touch).
- **guardrail violations** — `guardrails.count_violations`: an independent
  recomputation from the finished logs (retries > 3, messages > 2, a retry gap
  < 24h, a message outside 09:00–20:00, or the action hard cap exceeded),
  counting only steps actually carried out. Structurally 0.
- **double charges** — a second gateway create for an idempotency key already
  seen. 0 by construction; the count of short-circuited duplicate calls is shown
  separately.
- **naive baseline** — `baseline.naive_one_retry`: one immediate retry, then
  give up. **blind baseline** — `baseline.blind_three`: three retries an hour
  apart. Both share the executor's outcome oracle and per-case seed, so they roll
  the same random numbers as the agent; only the decision logic differs. Neither
  respects the guardrails, which is the point of showing them.

## What broke, and how I got out

Four bugs worth keeping. All were caught by tests or by diffing the headline
number before/after a change - none by staring at the code.

**1. The rules table was *too good*, so the LLM never ran.**
The first messy-reason pool leaned on phrases like "expired card" and "e-mandate"
that the literal text rules matched anyway, so the LLM fallback classified ~0
cases and there was nothing to demonstrate. Fix: split the messy pool in two -
half carry a literal token the rules still catch, half have only semantic signal
("the paycheck is late this month") and fall through to the LLM. The diagnoser's
rules table was also deliberately trimmed back to literal tokens only. Now ~9% of
cases reach the LLM, which is the honest split.

**2. The fake gateway's random ids broke reproducibility.**
The local Razorpay fake minted ids with `random.random()`, so two runs of the
same seed produced different `order_*` / `plink_*` ids and
`result_to_dict` comparisons failed. Fix: derive the fake id from the
idempotency key (`sha256(key)[:14]`) - which is also what a real idempotent
create does, so the fake got *more* realistic, not less.

**3. The money safety ceiling was set ~40x too low.**
`MONEY.max_total_attempted_paise` was written as `100 * 5_000_00` (₹5,00,000)
when it was meant to be ₹2 crore. The `SpendGovernor` then blocked every
charge after the first fifty-odd cases, silently escalating two-thirds of the
batch - headline recovery dropped 58% → 23% during the step 7 refactor. Caught
by `git stash`-ing the change and re-running: the number moved when it
shouldn't have. Fix: correct the constant, and scope the ceiling to *retries*
(auto-charges) rather than customer-driven links.

**4. A payload key named `kind` shadowed the audit record type.**
`AuditSink._append(kind, payload)` did `{... "kind": kind, **payload}`, and the
`case_summary` payload also had a `"kind"` (the case kind, "payment"/
"subscription"). The spread overwrote the record type, so every summary line was
`"kind": "payment"` and the hash still verified (it hashes whatever's there).
Caught by a test asserting the record kinds. Fix: a `_RESERVED` set in
`_append` so `seq` / `ts` / `kind` / `prev_hash` / `hash` always win over the
payload.
