# Dunning Agent - pitch

Razorpay AI Buildathon, Track 3 (AI Revenue Recovery).

## The one-liner

An agent that takes a failed payment, works out *why* it failed, runs a bounded
recovery within the rules a real payments stack enforces, and proves - across a
300-case batch, ending on a rupee number - how much money it brought back.

## The 60-second version

Revenue rarely dies in one step. A card declines, a subscription charge bounces,
a checkout is abandoned. Each of those is money that *failed to arrive* and is
lost unless someone acts. Doing it by hand doesn't scale; retrying blindly is
worse - it hammers issuers, breaks autopay mandates, and tanks your success-rate
score.

The Dunning Agent closes the loop:

1. **Detect** - it consumes failed-payment webhooks (replayed from a signed
   file; every one is HMAC-verified on the way in).
2. **Diagnose** - it maps the failure to one of 15 root causes. 91% by lookup
   tables and literal rules; the messy ~9% of free text goes to an LLM, which
   *only labels* - it never picks an action or an amount. Anything it can't place
   with confidence becomes `needs_review` and goes to a human, never a guess.
3. **Decide** - a deterministic policy turns the cause + customer context into a
   fixed, bounded plan: an ordered list of interventions with a schedule. The
   cause→steps table is a readable YAML file. It never retries a security block
   or a dead mandate.
4. **Act** - it walks the plan against **real Razorpay test-mode APIs** (Orders,
   Payment Links), every call carrying an idempotency key. A virtual clock lets
   the ≥24h retry spacing and the 09:00-20:00 contact window actually bite. It
   status-checks before every charge.
5. **Measure** - across the batch: money recovered, recovery by cause, escalations,
   and a hash-chained audit log you can verify with one command.

## The number

Seed 42, 300 synthetic at-risk cases, ₹14,57,038 at risk. Deterministic;
regenerate with `make run`.

| Strategy | Recovered | % of at-risk | Within the rules |
|---|---|---|---|
| Naive - one blind retry | ₹3,32,679 | 22.8% | no |
| Blind - three retries 1h apart | ₹6,65,895 | 45.7% | no - breaks the ≥24h rule, hammers issuers |
| **Dunning Agent** | **₹7,98,285** | **54.9%** | **yes - 0 violations, 0 double charges** |

The blind strategies only out-score a single retry by doing things a real
payments stack forbids. The agent's number is the one you could ship.

## Where we deliberately did NOT use AI

The retry schedule, every limit and stopping rule, all money math, the "did it
arrive?" check, the audit log, and every metric are plain deterministic code -
they have to be reproducible and explainable line by line. The LLM does exactly
two narrow jobs: messy failure text → a label, and drafting the customer nudge
(per channel, per language, with a validator that blocks threats and PII leaks).

## Failures we handle on purpose

- **Mandate cancelled mid-sequence** - the agent detects the subscription mandate
  died, stops retrying (retrying a dead mandate breaches its terms), re-plans
  once to a re-authorisation link, then escalates.
- **Customer pays out of band while a retry is pending** - a status check before
  the charge finds the payment already completed; no second charge is created.

Both are planted in every batch and cited by `case_id` in the report.

## Guardrails & trust

- Caps enforced *before* each action, so a breach is structurally impossible;
  an independent recomputation over the finished logs confirms 0.
- Real Razorpay calls need an explicit `RAZORPAY_LIVE=1`; otherwise a local
  simulator, so the whole thing runs with zero setup.
- PII is masked everywhere it's logged or shown.
- `logs/audit.jsonl` is a hash chain with a signed manifest;
  `make verify-audit` catches any edit, drop, or reorder.

## Demo script (3 minutes)

1. `make run` - narrate the pipeline as it prints the headline.
2. Open `reports/latest.html` - hero %, the strategy table, recovery by cause
   (salary-day timing wins on `insufficient_funds`; nothing saves
   `stolen_or_lost_card` - honest).
3. Scroll to "Deliberate failures, handled" - walk the two timelines.
4. `make verify-audit` - "AUDIT VERIFIED", then edit one line of
   `logs/audit.jsonl` and run it again - "FAILED: hash mismatch".
5. `RANDOM_SEED=7 make run` - same story, different draw. Not one lucky seed.

## Build quality

~3,900 lines of implementation, ~1,600 of tests, 238 passing. One module per
concern, `make` targets for every stage, no service to stand up. See
`docs/architecture.md` for the component map, the metrics definitions, and the
four bugs that bit during the build.
