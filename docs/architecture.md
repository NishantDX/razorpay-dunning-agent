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
