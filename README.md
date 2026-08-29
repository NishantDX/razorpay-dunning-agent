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
2. **Diagnose** - map each raw failure reason to one canonical root cause
   (`insufficient_funds`, `expired_card`, `bank_timeout`, `mandate_cancelled`, `abandoned`).
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
make generate    # build the synthetic batch  -> data/cases.jsonl
make feed        # shape it into a webhook feed -> data/events.jsonl
make run         # run the agent over the batch -> reports/latest.html
make report      # open the report
```

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

**Step 3 of 13 complete:** the event feed (`dunning/feed.py`).

- **Step 2** — `make generate` writes ~300 seeded at-risk cases to
  `data/cases.jsonl` (+ `data/cases.meta.json`). Each carries ground-truth
  `root_cause`, a customer profile, a raw failure reason (~15% deliberately
  messy), and hidden `latent` parameters the executor rolls a seeded RNG against.
- **Step 3** — `make feed` turns each case into an event shaped like a real
  Razorpay webhook (`payment.failed`, `subscription.pending`, `order.abandoned`),
  ordered by failure time, written to `data/events.jsonl`. `feed.replay()` yields
  them one at a time, as if Razorpay were POSTing them to us. The hidden `latent`
  block is not copied into events — the agent only ever sees the webhook.

Next: the diagnoser (step 4).
