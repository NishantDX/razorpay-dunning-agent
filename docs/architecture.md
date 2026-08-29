# Architecture

> Draft. Filled in as the build progresses (step 13).

## The recovery loop (5 steps)

1. **Detect** - consume a stream of at-risk events (failed one-time payments, halted subscriptions).
2. **Diagnose** - map each raw failure reason to one canonical root cause.
3. **Decide** - a deterministic policy picks the intervention that fits the cause.
4. **Act, within bounds** - execute via Razorpay test APIs, check whether the money
   arrived, obey hard caps and stopping rules, log every step.
5. **Measure** - over the whole batch: rupees recovered, % recovered, avg attempts,
   count escalated, rule violations (target 0), double charges (target 0). Broken down
   by root cause, compared against a naive baseline.

## Components

_TODO: diagram + one line per component (generator, feed, diagnoser, policy engine,
executor, guardrails, message writer, audit log, batch runner)._

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

## Where we deliberately did NOT use an LLM, and why

The retry schedule, every limit and stopping rule, all money math, the "did it
arrive?" check, the audit log, and every metric are plain deterministic code.
Money decisions must be reproducible and explainable line by line; an LLM is
non-deterministic and hard to audit. Even inside the diagnoser, ~91% of events
are classified by lookup tables and literal rules — the LLM only sees the ~9%
of genuinely messy free text, and even then it only *labels*: it never chooses
an action, a schedule, or an amount. The other LLM use is writing the customer
nudge message (step 8), where natural phrasing is the point.

## Guardrails

_TODO: pull from `dunning/config.py` - max 3 retries, >=24h apart, contact only
09:00-20:00, <=2 messages/customer, absolute cap of 5 actions. Stopping rules: money
recovered, customer reply (paid / stop / dispute / unsubscribe), dead mandate,
max retries, human escalation._

## Metrics definitions

_TODO: define each headline number precisely (what counts as "recovered", how attempts
are counted, what "escalated" means, how the naive baseline is computed)._

## What broke, and how I got out

_TODO: dev journal - the real bug hit while building + the fix._
