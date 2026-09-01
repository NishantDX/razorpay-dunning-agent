"""Step 2 - synthetic at-risk case generator.

Produces ``data/cases.jsonl``: ~300 "revenue is slipping away" cases, each one
either a failed one-time payment or a failed subscription charge. Every case
carries four things:

* what the diagnoser will see   -> ``raw_failure_reason`` (~15% is messy free text)
* ground truth for scoring      -> ``root_cause``
* a customer profile            -> ``customer`` (reachable?, history, language)
* hidden probabilistic params   -> ``latent``: the executor (step 6) rolls a
  *seeded* RNG against these at each attempt to decide whether money actually
  arrives. This is what bakes in the patterns a disciplined agent exploits and
  a naive one misses:
    - insufficient_funds recovers far more often when retried near the
      customer's salary day (``funds_return_day``)
    - an expired card never clears on retry - you must switch method / send a link
    - a bank timeout is usually transient - one quick retry often works
    - a cancelled mandate must never be auto-retried - link + human only
    - an abandoned checkout has nothing to retry - a payment link is the only move

Reproducibility: everything derives from the seed. Each case gets its own RNG
seeded by ``sha256(seed : case_id)``, so a case's data and hidden outcome do not
depend on how many cases were generated before it or in what order.

Run:  python -m dunning.generate  [--count N] [--seed S] [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import string
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from faker import Faker

from dunning import config

GENERATOR_VERSION = 2

# --------------------------------------------------------------------------- #
# Generation knobs - the tunable mix. The *actual* resulting split is printed
# after every run and written to data/cases.meta.json, so these stay honest.
# --------------------------------------------------------------------------- #

# one-time payments vs subscription charges
KIND_WEIGHTS = {"payment": 0.65, "subscription": 0.35}

# root cause, conditional on kind. Keys are real failure causes from
# config.ROOT_CAUSES ("needs_review" is a diagnoser state, never generated).
# Weights are rough real-world frequencies: plain declines dominate, security
# blocks are rare.
ROOT_CAUSE_WEIGHTS = {
    "payment": {
        "insufficient_funds": 0.22,
        "do_not_honour": 0.20,
        "bank_timeout": 0.10,
        "three_ds_failed": 0.10,
        "expired_card": 0.09,
        "card_limit_exceeded": 0.07,
        "technical_decline": 0.05,
        "issuer_unavailable": 0.05,
        "abandoned": 0.05,
        "invalid_payment_details": 0.03,
        "international_blocked": 0.02,
        "card_declined_risk": 0.015,
        "stolen_or_lost_card": 0.005,
    },
    "subscription": {
        "insufficient_funds": 0.30,
        "mandate_cancelled": 0.20,
        "do_not_honour": 0.15,
        "expired_card": 0.10,
        "bank_timeout": 0.08,
        "issuer_unavailable": 0.05,
        "three_ds_failed": 0.04,
        "card_limit_exceeded": 0.04,
        "technical_decline": 0.03,
        "card_declined_risk": 0.01,
    },
}

# fraction of cases whose raw_failure_reason is messy free text rather than a
# clean bank / gateway description (exercises the diagnoser's LLM fallback).
MESSY_REASON_RATE = 0.15

# fraction of recoverable-looking subscription cases where the mandate is
# revoked partway through the sequence (step 12's deliberate failure).
MANDATE_REVOKE_RATE = 0.20
# fraction of retry-safe cases where the customer pays out of band mid-sequence
OUT_OF_BAND_PAY_RATE = 0.05

LANGUAGE_WEIGHTS = {"en": 0.45, "hinglish": 0.40, "hi": 0.15}
REACHABLE_RATE = 0.85

# amount range in whole rupees, by kind (subscriptions skew smaller / recurring)
AMOUNT_RANGE = {"payment": (99, 50000), "subscription": (149, 4999)}

# failures are spread across this many days before the reference "now"
FAILURE_WINDOW_DAYS = 30

IST = timezone(timedelta(hours=5, minutes=30))
# Fixed reference point so failed_at timestamps are reproducible run to run.
REFERENCE_NOW = datetime(2026, 8, 29, 10, 0, tzinfo=IST)

# hour-of-day weights for when the original payment failed (daytime-heavy)
_HOUR_WEIGHTS = [1, 1, 1, 1, 1, 2, 3, 5, 7, 9, 10, 10, 9, 9, 9, 9, 10, 11, 11, 10, 8, 6, 4, 2]

# --------------------------------------------------------------------------- #
# Failure-reason text pools
# --------------------------------------------------------------------------- #

# Clean reasons read like a bank / gateway description string - the kind that
# arrives with a usable structured error_reason code alongside it.
_CLEAN_REASONS = {
    "insufficient_funds": [
        "Your payment failed. Reason: insufficient funds in the account.",
        "Payment declined by bank - insufficient balance.",
        "Transaction failed: account balance too low to complete the payment.",
    ],
    "card_limit_exceeded": [
        "Payment declined: the card's transaction limit has been exceeded.",
        "This amount is over the daily spending limit set on the card.",
        "Declined - withdrawal frequency limit reached for this card.",
    ],
    "bank_timeout": [
        "The bank did not respond in time. Please retry the payment.",
        "Payment failed due to a timeout at the bank's end. No amount was deducted.",
        "Gateway timeout while authorising the payment.",
    ],
    "issuer_unavailable": [
        "The issuing bank is currently unavailable. Please try again later.",
        "Payment failed: issuer reported as inoperative.",
        "The card issuer's systems are down right now.",
    ],
    "technical_decline": [
        "Payment failed due to a technical error at the gateway.",
        "The transaction could not be processed - processor technical decline.",
        "A technical problem stopped this payment. No amount was deducted.",
    ],
    "three_ds_failed": [
        "Payment failed: 3-D Secure authentication was not completed.",
        "The OTP / additional authentication step failed.",
        "Card authentication failed; the payment was not authorised.",
    ],
    "expired_card": [
        "Your card has expired. Please try again with a different card.",
        "Payment failed because the card has expired.",
        "The card used has expired; please use another payment method.",
    ],
    "invalid_payment_details": [
        "Payment failed: the card details entered are invalid.",
        "Declined - invalid card number or security code.",
        "The bank could not find an account matching these card details.",
    ],
    "international_blocked": [
        "International cards are not accepted for this payment.",
        "Payment declined: cross-border transactions are blocked on this card.",
        "This card's country is not supported for this transaction.",
    ],
    "do_not_honour": [
        "Payment declined by the issuing bank (do not honour).",
        "The bank refused the charge without giving a specific reason.",
        "Declined by issuer - reason code 05, do not honour.",
    ],
    "card_declined_risk": [
        "Payment blocked by the bank's fraud checks.",
        "Declined - the issuer flagged this transaction as high risk.",
        "The bank declined this payment for security reasons.",
    ],
    "stolen_or_lost_card": [
        "Payment declined: the card has been reported lost or stolen.",
        "Declined by issuer - card reported stolen.",
        "The bank has blocked this card (reported lost).",
    ],
    "mandate_cancelled": [
        "The e-mandate for this subscription has been cancelled by the customer.",
        "Recurring charge failed: autopay mandate revoked at the bank.",
        "The autopay mandate is no longer active for this subscription.",
    ],
    "abandoned": [
        "Customer did not complete the payment.",
        "Checkout was started but no payment was attempted.",
        "Order was created; the customer left before paying.",
    ],
}

# Messy free-text reasons - the kind a support agent or a terse bank webhook
# leaves. Per cause: the first two carry a literal token the diagnoser's rules
# table can still catch; the rest have only *semantic* signal and fall through
# to the LLM classifier.
_MESSY_REASONS = {
    "insufficient_funds": [
        "txn declnd - insuff bal, cust says salary comes on 1st",
        "NACH return - reason code: 'insufficient funds'",
        "bank says the account isn't funded enough this cycle",
        "customer told us the paycheck is late this month",
        "not enough balance to cover it right now",
    ],
    "card_limit_exceeded": [
        "resp 61 exceeds withdrawal limit",
        "over the per-txn cap on this card",
        "customer has spent up to their card ceiling this month",
        "amount is above what the bank lets this card do in a day",
    ],
    "bank_timeout": [
        "UPI timeout @ NPCI, RRN not generated, maybe retry",
        "no response frm issuer after 30s, socket closed",
        "gateway hiccup, nothing was deducted, probably retryable",
        "the bank's rail was slow and the call never came back",
    ],
    "issuer_unavailable": [
        "issuer down - resp 91, try again later",
        "bank not available right now per the switch",
        "the customer's bank looks offline, lots of these today",
        "card network says the issuer isn't reachable at the moment",
    ],
    "technical_decline": [
        "GATEWAY_ERROR - technical decline, no funds moved",
        "processor threw a technical error mid-capture",
        "something broke on the processing side, not the customer",
        "internal error while putting the charge through",
    ],
    "expired_card": [
        "resp code 54 expired card, pls ask cust to update",
        "vault card dead, exp date in past",
        "the card on file is too old now, issuer won't take it",
        "need the customer to add a fresh card, current one won't go through",
    ],
    "three_ds_failed": [
        "3ds auth failed, cust didn't finish OTP",
        "AFA step incomplete on this one",
        "customer never entered the code the bank sent",
        "the extra verification the bank asked for did not go through",
    ],
    "invalid_payment_details": [
        "resp 14 invalid card number",
        "card details don't check out, bad CVV maybe",
        "the number the customer typed doesn't map to a real account",
        "wrong details entered at checkout, bank can't match them",
    ],
    "international_blocked": [
        "intl card, cross-border not enabled",
        "foreign card - blocked for this merchant",
        "the customer is paying with an overseas card we can't take",
        "card issued outside India, not allowed on this transaction",
    ],
    "do_not_honour": [
        "resp 05 do not honour",
        "DNH from issuer, no reason given",
        "the bank just said no without telling us why",
        "issuer refused it and gave nothing back to work with",
    ],
    "card_declined_risk": [
        "resp 59 suspected fraud, do not retry",
        "issuer risk block on this txn",
        "bank's fraud engine stopped this, leave it alone",
        "flagged as risky by the issuer, must not attempt again",
    ],
    "stolen_or_lost_card": [
        "resp 43 stolen card - pickup",
        "card reported lost, issuer blocked it",
        "the bank says this card was flagged as compromised",
        "issuer wants the card seized, definitely no retry",
    ],
    "mandate_cancelled": [
        "sub charge fail: mandate status = REVOKED",
        "auto-debit bounced, e-mandate not active anymore",
        "customer pulled the recurring permission from their bank app",
        "standing instruction is no longer on file at the bank",
    ],
    "abandoned": [
        "order stuck in 'created', 0 payment attempts logged",
        "link opened, no txn - customer dropped at the OTP screen",
        "checkout was opened but the customer walked away before paying",
        "customer left the page, nothing was charged",
    ],
}

_ID_ALPHABET = string.ascii_letters + string.digits


# --------------------------------------------------------------------------- #
# Per-case helpers (all draws come from the case-local RNG)
# --------------------------------------------------------------------------- #

def _case_rng(seed: int, case_id: str) -> random.Random:
    """A random.Random seeded so a case's data + hidden outcome depend only on
    (seed, case_id) - never on generation order or batch size."""
    digest = hashlib.sha256(f"{seed}:{case_id}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def _weighted(rng: random.Random, mapping: dict) -> str:
    keys = list(mapping)
    return rng.choices(keys, weights=[mapping[k] for k in keys])[0]


def _rand_id(rng: random.Random, prefix: str, n: int = 14) -> str:
    return prefix + "".join(rng.choice(_ID_ALPHABET) for _ in range(n))


def _amount_rupees(rng: random.Random, kind: str) -> int:
    lo, hi = AMOUNT_RANGE[kind]
    amt = math.exp(rng.uniform(math.log(lo), math.log(hi)))  # log-uniform: skewed low
    if amt < 500:
        step = 1
    elif amt < 2000:
        step = 10
    elif amt < 10000:
        step = 50
    else:
        step = 100
    amt = int(round(amt / step) * step)
    return max(lo, min(hi, amt))


def _failed_at(rng: random.Random, reference_now: datetime) -> datetime:
    days = rng.randint(1, FAILURE_WINDOW_DAYS)
    hour = rng.choices(range(24), weights=_HOUR_WEIGHTS)[0]
    minute = rng.randint(0, 59)
    dt = reference_now - timedelta(days=days)
    return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _raw_reason(rng: random.Random, root_cause: str) -> tuple:
    if rng.random() < MESSY_REASON_RATE:
        return rng.choice(_MESSY_REASONS[root_cause]), True
    return rng.choice(_CLEAN_REASONS[root_cause]), False


def _build_customer(rng: random.Random, faker: Faker, kind: str) -> dict:
    if kind == "subscription":
        prior_payments = rng.randint(1, 24)  # they had a running subscription
    else:
        prior_payments = rng.choices(
            [0, 1, 2, 3, 5, 8, 12], weights=[3, 4, 4, 3, 2, 2, 1]
        )[0]
    return {
        "customer_id": _rand_id(rng, "cust_"),
        "name": faker.name(),
        "email": faker.safe_email(),
        "contact": "+91" + str(rng.randint(6, 9))
        + "".join(str(rng.randint(0, 9)) for _ in range(9)),
        "language": _weighted(rng, LANGUAGE_WEIGHTS),
        "reachable": rng.random() < REACHABLE_RATE,
        "prior_payments": prior_payments,
        "prior_success_rate": round(rng.uniform(0.55, 0.98), 2),
    }


def _build_subscription(rng: random.Random, root_cause: str, amount_paise: int) -> dict:
    return {
        "subscription_id": _rand_id(rng, "sub_"),
        "plan_id": _rand_id(rng, "plan_", 12),
        "invoice_id": _rand_id(rng, "inv_"),
        "recurring_amount_paise": amount_paise,
        "billing_cycle": rng.choice(["monthly", "monthly", "monthly", "weekly", "yearly"]),
        "charge_attempt": rng.randint(2, 18),  # not their first charge
        "mandate_status": "cancelled" if root_cause == "mandate_cancelled" else "active",
    }


def _build_latent(rng: random.Random, root_cause: str, kind: str) -> dict:
    """Hidden parameters the executor rolls against. All probabilities are for a
    single, well-targeted attempt; the executor combines them with timing and
    reachability at run time."""
    latent = {
        "base_recovery_prob": 0.0,        # a spaced retry of the same charge, no bonus
        "funds_return_day": None,         # day-of-month money reliably lands (salary)
        "timing_bonus_prob": 0.0,         # added when retried within ~1 day of that
        "transient_retry_prob": 0.0,      # an immediate retry_now clears it (blip)
        "limit_resets": False,            # a retry after ~24h clears it (daily limit)
        "method_dead": False,             # retrying the same instrument can never work
        "link_response_prob": 0.0,        # customer pays if sent a payment link
        "mandate_link_prob": 0.0,         # customer re-authorises if sent a mandate link
        "opt_out_prob": 0.02,             # customer replies stop / unsubscribe on a msg
        "chronic": False,                 # unrecoverable no matter what the agent does
        "mandate_revokes_at_attempt": None,  # mandate dies mid-sequence at this attempt
        "paid_out_of_band_at_action": None,  # customer pays independently before the
                                             # agent's Nth action - a status check must
                                             # catch it so no second charge is created
        "showcase": None,                 # tags a deliberately-planted edge case
    }

    if root_cause == "insufficient_funds":
        latent["funds_return_day"] = rng.choice([1, 1, 2, 3, 5, 28, 30])
        latent["base_recovery_prob"] = 0.12
        latent["timing_bonus_prob"] = 0.78          # retrying near salary day
        latent["link_response_prob"] = 0.30
        if rng.random() < 0.15:
            latent["chronic"] = True
            latent["timing_bonus_prob"] = 0.10
            latent["link_response_prob"] = 0.08

    elif root_cause == "card_limit_exceeded":
        latent["limit_resets"] = True               # daily limit clears overnight
        latent["base_recovery_prob"] = 0.20
        latent["link_response_prob"] = 0.45         # or pay with another method
        if rng.random() < 0.20:                      # a hard credit-limit wall
            latent["limit_resets"] = False
            latent["base_recovery_prob"] = 0.05

    elif root_cause == "bank_timeout":
        latent["transient_retry_prob"] = 0.70 if rng.random() > 0.20 else 0.15
        latent["base_recovery_prob"] = 0.72
        latent["link_response_prob"] = 0.40
        if rng.random() < 0.10:
            latent["chronic"] = True
            latent["base_recovery_prob"] = 0.05
            latent["transient_retry_prob"] = 0.05

    elif root_cause == "issuer_unavailable":
        latent["transient_retry_prob"] = 0.10       # too soon, still down
        latent["base_recovery_prob"] = 0.68         # clears after a longer wait
        latent["link_response_prob"] = 0.35
        if rng.random() < 0.12:
            latent["chronic"] = True
            latent["base_recovery_prob"] = 0.06

    elif root_cause == "technical_decline":
        latent["transient_retry_prob"] = 0.55
        latent["base_recovery_prob"] = 0.60
        latent["link_response_prob"] = 0.40

    elif root_cause == "three_ds_failed":
        latent["base_recovery_prob"] = 0.10         # silent retry rarely helps
        latent["link_response_prob"] = 0.60         # they finish OTP on the link
        if rng.random() < 0.15:
            latent["link_response_prob"] = 0.15

    elif root_cause == "expired_card":
        latent["method_dead"] = True
        latent["base_recovery_prob"] = 0.02
        latent["link_response_prob"] = 0.55 if rng.random() > 0.20 else 0.10

    elif root_cause == "invalid_payment_details":
        latent["method_dead"] = True
        latent["base_recovery_prob"] = 0.0
        latent["link_response_prob"] = 0.55 if rng.random() > 0.25 else 0.12

    elif root_cause == "international_blocked":
        latent["method_dead"] = True
        latent["base_recovery_prob"] = 0.02
        latent["link_response_prob"] = 0.50 if rng.random() > 0.25 else 0.12

    elif root_cause == "do_not_honour":
        latent["base_recovery_prob"] = 0.15         # occasionally clears, mostly not
        latent["link_response_prob"] = 0.45         # a different card works
        if rng.random() < 0.25:
            latent["chronic"] = True
            latent["base_recovery_prob"] = 0.04
            latent["link_response_prob"] = 0.10

    elif root_cause == "card_declined_risk":
        latent["method_dead"] = True               # retrying is harmful, never works
        latent["base_recovery_prob"] = 0.0
        latent["link_response_prob"] = 0.35 if rng.random() > 0.35 else 0.05

    elif root_cause == "stolen_or_lost_card":
        latent["method_dead"] = True
        latent["base_recovery_prob"] = 0.0
        latent["link_response_prob"] = 0.15        # mostly these just don't recover
        if rng.random() < 0.6:
            latent["chronic"] = True

    elif root_cause == "mandate_cancelled":
        latent["method_dead"] = True  # auto-retry here is also a policy breach
        latent["base_recovery_prob"] = 0.0
        latent["mandate_link_prob"] = 0.40 if rng.random() > 0.25 else 0.08

    elif root_cause == "abandoned":
        latent["base_recovery_prob"] = 0.0  # nothing to retry
        latent["link_response_prob"] = 0.35 if rng.random() > 0.25 else 0.10

    # a mandate that dies partway through an otherwise-recoverable subscription:
    # the executor sees it on the Nth action and must re-plan / stop.
    if (
        kind == "subscription"
        and root_cause in ("insufficient_funds", "bank_timeout", "do_not_honour",
                           "issuer_unavailable", "technical_decline")
        and not latent["chronic"]
        and rng.random() < MANDATE_REVOKE_RATE
    ):
        latent["mandate_revokes_at_attempt"] = rng.choice([1, 2, 2])

    # the customer pays out of band (their own bank app / an earlier link) while a
    # retry is still scheduled - the agent must status-check before charging again.
    if (
        root_cause in config.RETRY_SAFE_CAUSES
        and not latent["chronic"]
        and latent["mandate_revokes_at_attempt"] is None
        and rng.random() < OUT_OF_BAND_PAY_RATE
    ):
        latent["paid_out_of_band_at_action"] = rng.choice([2, 2, 3])

    return latent


# --------------------------------------------------------------------------- #
# Case + batch assembly
# --------------------------------------------------------------------------- #

def _build_case(index: int, seed: int, reference_now: datetime) -> dict:
    case_id = f"case_{index:04d}"
    rng = _case_rng(seed, case_id)
    faker = Faker()
    faker.seed_instance(int(hashlib.sha256(case_id.encode()).hexdigest()[:12], 16))

    kind = _weighted(rng, KIND_WEIGHTS)
    root_cause = _weighted(rng, ROOT_CAUSE_WEIGHTS[kind])

    amount_rupees = _amount_rupees(rng, kind)
    amount_paise = amount_rupees * 100
    failed_at = _failed_at(rng, reference_now)
    reason, reason_is_messy = _raw_reason(rng, root_cause)
    customer = _build_customer(rng, faker, kind)
    latent = _build_latent(rng, root_cause, kind)

    is_sub = kind == "subscription"
    return {
        "schema_version": 1,
        "case_id": case_id,
        "kind": kind,
        "source": "synthetic",
        "amount_paise": amount_paise,
        "amount_rupees": amount_rupees,
        "currency": "INR",
        "failed_at": failed_at.isoformat(),
        "root_cause": root_cause,          # ground truth; the diagnoser must recover this
        "raw_failure_reason": reason,
        "reason_is_messy": reason_is_messy,
        "customer": customer,
        "order_id": None if is_sub else _rand_id(rng, "order_"),
        # abandoned checkouts never produced a payment object
        "payment_id": (
            None if (is_sub or root_cause == "abandoned") else _rand_id(rng, "pay_")
        ),
        "subscription": (
            _build_subscription(rng, root_cause, amount_paise) if is_sub else None
        ),
        "latent": latent,
    }


def _ensure_showcases(cases: list) -> None:
    """Designate exactly one case per deliberately-handled edge and force its
    latent so the handling is *demonstrated* in every batch, whatever the seed.
    Picks a naturally-occurring candidate where possible, else the first fit.
    Mutates in place."""

    def _quiet(latent):  # so the trigger is actually reached, not pre-empted
        latent["base_recovery_prob"] = 0.0
        latent["timing_bonus_prob"] = 0.0
        latent["transient_retry_prob"] = 0.0
        latent["chronic"] = False

    # 1. mandate revoked mid-sequence
    pick = next((c for c in cases if c["latent"]["mandate_revokes_at_attempt"] is not None
                 and not c["latent"]["showcase"]), None)
    pick = pick or next((c for c in cases if c["kind"] == "subscription"
                         and c["root_cause"] in config.RETRY_SAFE_CAUSES
                         and not c["latent"]["showcase"]), None)
    if pick:
        _quiet(pick["latent"])
        pick["latent"]["mandate_revokes_at_attempt"] = 2
        pick["latent"]["paid_out_of_band_at_action"] = None
        pick["latent"]["showcase"] = "mandate_revoked_midway"

    # 2. customer pays out of band while a retry is pending
    pick = next((c for c in cases if c["latent"]["paid_out_of_band_at_action"] is not None
                 and not c["latent"]["showcase"]), None)
    pick = pick or next((c for c in cases if c["kind"] == "payment"
                         and c["root_cause"] in config.RETRY_SAFE_CAUSES
                         and c["customer"]["reachable"] and not c["latent"]["showcase"]), None)
    if pick:
        _quiet(pick["latent"])
        pick["latent"]["mandate_revokes_at_attempt"] = None
        pick["latent"]["paid_out_of_band_at_action"] = 2
        pick["latent"]["showcase"] = "double_charge_prevented"


def generate_cases(count: int, seed: int = None, reference_now: datetime = REFERENCE_NOW):
    """Return a list of ``count`` case dicts. Deterministic (count is only the
    range; each case depends on ``seed`` and its own id). At least one instance of
    each deliberately-handled edge case is guaranteed and tagged in ``latent.showcase``."""
    if seed is None:
        seed = config.RANDOM_SEED
    cases = [_build_case(i, seed, reference_now) for i in range(count)]
    _ensure_showcases(cases)
    return cases


def write_cases(cases, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")


def _distribution(cases) -> dict:
    by_kind = Counter(c["kind"] for c in cases)
    by_cause = Counter(c["root_cause"] for c in cases)
    by_language = Counter(c["customer"]["language"] for c in cases)
    return {
        "by_kind": dict(by_kind),
        "by_root_cause": dict(by_cause),
        "by_language": dict(by_language),
        "messy_reasons": sum(c["reason_is_messy"] for c in cases),
        "unreachable_customers": sum(not c["customer"]["reachable"] for c in cases),
        "chronic_unrecoverable": sum(c["latent"]["chronic"] for c in cases),
        "mandate_revokes_midway": sum(
            c["latent"]["mandate_revokes_at_attempt"] is not None for c in cases
        ),
        "total_at_risk_rupees": sum(c["amount_rupees"] for c in cases),
    }


def _write_meta(cases, out_path: Path, seed: int, reference_now: datetime) -> Path:
    meta = {
        "generator_version": GENERATOR_VERSION,
        "generated_at": datetime.now(IST).isoformat(),
        "seed": seed,
        "count": len(cases),
        "reference_now": reference_now.isoformat(),
        "cases_file": str(out_path),
        "distribution": _distribution(cases),
    }
    meta_path = Path(out_path).with_name("cases.meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta_path


def _print_summary(cases, out_path: Path, meta_path: Path) -> None:
    dist = _distribution(cases)
    print(f"\nGenerated {len(cases)} cases -> {out_path}")
    print(f"metadata               -> {meta_path}")
    print("\n  by kind:")
    for k, v in sorted(dist["by_kind"].items()):
        print(f"    {k:<14} {v:>4}  ({v / len(cases):.0%})")
    print("  by root cause:")
    for k, v in sorted(dist["by_root_cause"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<20} {v:>4}  ({v / len(cases):.0%})")
    print("  by language:")
    for k, v in sorted(dist["by_language"].items(), key=lambda kv: -kv[1]):
        print(f"    {k:<14} {v:>4}  ({v / len(cases):.0%})")
    print(
        "\n  messy free-text reasons : {messy_reasons}"
        "\n  unreachable customers   : {unreachable_customers}"
        "\n  chronic (unrecoverable) : {chronic_unrecoverable}"
        "\n  mandate dies mid-seq    : {mandate_revokes_midway}"
        "\n  total at-risk value     : Rs {total_at_risk_rupees:,}".format(**dist)
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Generate the synthetic at-risk batch.")
    parser.add_argument("--count", type=int, default=config.BATCH_SIZE,
                        help=f"number of cases (default {config.BATCH_SIZE})")
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED,
                        help=f"RNG seed (default {config.RANDOM_SEED})")
    parser.add_argument("--out", type=Path, default=config.CASES_FILE,
                        help=f"output JSONL path (default {config.CASES_FILE})")
    args = parser.parse_args(argv)

    cases = generate_cases(args.count, seed=args.seed)
    write_cases(cases, args.out)
    meta_path = _write_meta(cases, args.out, args.seed, REFERENCE_NOW)
    _print_summary(cases, args.out, meta_path)


if __name__ == "__main__":
    main()
