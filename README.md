# Dunning Agent

**Find revenue that's slipping away and win it back.**

An agent that consumes a stream of at-risk payment events, diagnoses the root cause,
picks a bounded recovery action, executes it against Razorpay test-mode APIs, and
proves how much money it recovered across a whole batch - with hard guardrails,
stopping rules, and an append-only audit trail.

Built for Track 3 (AI Revenue Recovery) of the Razorpay AI Buildathon.

---

## The loop

**Detect -> Diagnose -> Decide -> Act (within bounds) -> Measure**

1. **Detect** - replay ~300 synthetic at-risk events (failed one-time payments + halted subscriptions).
2. **Diagnose** - map each raw failure reason to one of 15 canonical root causes,
   grouped by how recovery must work: retry-safe (`insufficient_funds`,
   `bank_timeout`, `issuer_unavailable`, ...), needs-a-new-method (`expired_card`,
   `international_blocked`, ...), never-retry (`card_declined_risk`,
   `stolen_or_lost_card`, `mandate_cancelled`), `abandoned`, and `needs_review`
   when the diagnoser isn't confident.
3. **Decide** - a deterministic policy table picks the intervention
   (retry later / retry now / switch method / payment link / new mandate link / hand to human / do nothing).
4. **Act** - execute via Razorpay test APIs with idempotency keys; after each attempt,
   fetch payment status to check whether the money actually arrived. Obey hard caps and stopping rules.
5. **Measure** - over the batch: rupees recovered, % recovered, avg attempts, count escalated,
   rule violations (target 0), double charges (target 0). Broken down by root cause,
   and compared against a naive "one retry then give up" baseline.

## Where AI is / isn't used

| Job | Tool |
|---|---|
| Normalise messy free-text failure reasons -> clean root cause | **LLM** (fallback only; a rules table handles the common cases) |
| Write the customer nudge message (email / SMS / WhatsApp, Hinglish option) | **LLM** |
| Retry schedule, every limit + stopping rule, all money math, the "did it arrive?" check, the audit log, all metrics | **Plain deterministic code** |

Rationale in [docs/architecture.md](docs/architecture.md).

## Setup

Requires Python 3.9+, a free [Gemini API key](https://aistudio.google.com), and
[Razorpay test-mode keys](https://dashboard.razorpay.com) (`rzp_test_...`).

```bash
make setup                 # create .venv, install deps
cp .env.example .env       # then fill in your keys
```

## Run

```bash
make generate    # build the synthetic batch   -> data/cases.jsonl
make feed        # shape it into a webhook feed -> data/events.jsonl
make diagnose    # root-cause every event, scored vs ground truth
make policy      # plan a bounded recovery for every event
make execute     # diagnose -> plan -> execute against Razorpay test APIs
make run         # run the agent over the batch -> reports/latest.html
make report      # open the report
```

Without a `GEMINI_API_KEY` the diagnoser's LLM fallback uses a small offline
heuristic so everything runs with zero setup; set the key in `.env` for the real
Gemini classifier.

## Headline result

_Filled in after the first full run._

| Strategy | Recovered | % of at-risk value | Avg attempts | Escalated | Rule violations |
|---|---|---|---|---|---|
| Naive (one retry, then give up) | TBD | TBD | TBD | TBD | TBD |
| **Dunning Agent** | **TBD** | **TBD** | TBD | TBD | **0** |

## Guardrails

Max 3 retries per case, >=24h apart, contact only 09:00-20:00, <=2 messages per customer,
absolute backstop of 5 actions. The sequence stops on: money recovered, customer reply
(`paid` / `stop` / `dispute` / `unsubscribe`), a dead subscription mandate, max retries,
or escalation to a human. All defined in one place: [dunning/config.py](dunning/config.py).

## The failure we handle on purpose

A subscription whose mandate is cancelled mid-sequence: the agent detects the mandate is
dead, **stops retrying** (retrying would breach mandate terms), falls back to a one-time
Payment Link, and if that also fails, **escalates to a human** with a full case summary.

## What broke, and how I got out

_Dev journal kept in [docs/architecture.md](docs/architecture.md)._

## Repo layout

```
dunning/
  config.py          central config: env, guardrail limits, stopping rules, vocabulary
  generate.py        synthetic at-risk case generator (seeded)          [step 2]
  feed.py            replays cases as an event stream                   [step 3]
  diagnose.py        raw failure reason -> canonical root cause         [step 4]
  policy.py          root cause + customer context -> intervention plan [step 5]
  execute.py         carries out steps via Razorpay test APIs           [step 6]
  guardrails.py      enforces hard caps + stopping rules                [step 7]
  messaging.py       LLM writes the customer nudge                      [step 8]
  audit.py           append-only JSONL audit log                       [step 9]
  run_batch.py       runs the whole batch, writes the HTML report      [step 10]
config/policy.yaml   the root-cause -> intervention table
docs/architecture.md the architecture doc
```

## Status

**Step 5 of 13 complete:** the policy engine (`dunning/policy.py` +
`config/policy.yaml`).

- **Step 2** — `make generate`: ~300 seeded at-risk cases → `data/cases.jsonl`,
  each with ground-truth `root_cause`, a customer profile, a raw failure reason
  (~15% deliberately messy), and hidden `latent` recovery parameters.
- **Step 3** — `make feed`: each case → a Razorpay-webhook-shaped event,
  time-ordered, → `data/events.jsonl`; `feed.replay()` streams them one at a
  time. `latent` never enters an event.
- **Step 4** — `make diagnose`: a cheapest-first cascade maps each event to one
  of **15 canonical root causes** — event type, Razorpay `error_reason` code, a
  literal text-rules table, then an **LLM fallback** (Gemini) for messy free
  text. The only place an LLM touches a money decision, and it only *labels*.
  Cached by input-hash. ~99% against ground truth offline; unplaceable text →
  `needs_review`, never a guess.
- **Step 5** — `make policy`: turns a diagnosis + customer/subscription context
  into a **fixed, bounded plan** — an ordered list of interventions with a
  schedule. The cause→steps table lives in `config/policy.yaml`; context rules
  live in `policy.py`. Never retries a security block or a dead mandate; every
  plan ends in a human handoff or a deliberate write-off; hard-capped at 3
  retries / 2 messages.
- **Step 6** — `make execute`: walks each plan against **real Razorpay
  test-mode APIs** (`retry_*` creates a fresh Order, `send_*_link` creates a
  Payment Link), every create carrying an idempotency key the gateway dedupes on
  so a step can't double-charge. A virtual clock is fast-forwarded between steps.
  The *outcome* of an attempt (did the customer actually pay) is the one
  simulated part — rolled from the case's hidden `latent` with a seeded RNG.
- **Security pass** — PII masked everywhere it's logged; real Razorpay calls need
  an explicit `RAZORPAY_LIVE=1`; every replayed webhook is HMAC-signed and
  verified on the way in; the LLM prompt fences the failure text as untrusted
  data; out-of-bounds amounts are blocked before any API call; the idempotency
  map persists to disk so a re-run can't double-charge.
- **Step 7** — `dunning/guardrails.py`: one `GuardrailLedger` per case answers
  *allow / defer / skip / halt* before every step, so a breach is structurally
  impossible; every decision is kept for the audit log. A `SpendGovernor` caps
  the money a run may auto-charge and trips a circuit breaker on repeated gateway
  errors. An independent `assert_no_violations` recomputes from the finished logs
  — **0**, every run.
- **Step 8** — `dunning/messaging.py`: the LLM's second job — the customer nudge,
  written **per channel** (SMS terse, WhatsApp conversational) and per language
  (en / hi / hinglish). A deterministic validator checks every message states
  the amount, carries the link, and never threatens or leaks contact details;
  anything that fails, or a run with no API key, falls back to a safe template.

Next: the audit log (step 9).
