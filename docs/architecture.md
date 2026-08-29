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

## Where we deliberately did NOT use an LLM, and why

_TODO. Short version: the retry schedule, every limit and stopping rule, all money math,
the "did it arrive?" check, the audit log, and the metrics are plain deterministic code.
An LLM is non-deterministic and hard to audit; money decisions must be reproducible and
explainable line by line. The LLM is used only where fuzziness is the point:
(a) normalising messy free-text failure reasons, (b) writing the customer message._

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
