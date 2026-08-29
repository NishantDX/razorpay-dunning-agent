"""Step 3 - event feed.

The agent's job starts with a failure event. In production that event would
arrive as a **Razorpay webhook**: an HTTP POST Razorpay sends to our server the
moment a payment fails or a subscription charge bounces. We can't receive those
here - there is no public server and no real failing payments in test mode - so
we do what the brief says: replay them from a file.

This module turns each synthetic case (step 2) into an event shaped like a real
Razorpay webhook delivery, orders them by when the failure happened, and writes
them to ``data/events.jsonl``. ``replay()`` then yields them one at a time, in
time order, exactly as if Razorpay were POSTing them to us.

What the agent is allowed to see: only the event. The hidden ``latent`` block
from the case is deliberately NOT copied into the event - that is the executor's
private oracle (step 6). The case is linked back via ``notes.case_id`` (Razorpay
``notes`` is a real free-form field merchants use for their own IDs).

Event types emitted:
  * payment.failed        - a failed one-time payment            (real Razorpay event)
  * subscription.pending  - a failed recurring charge, will retry (real Razorpay event)
  * order.abandoned       - checkout started, never paid          (our own signal:
                            Razorpay fires nothing for a pure abandonment, so this
                            stands in for our own "order stuck in created" monitor)

Run:  python -m dunning.feed  [--cases PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from dunning import config

FEED_VERSION = 1

# a stand-in for the merchant account id that real webhooks carry
ACCOUNT_ID = "acc_SYNTH0000000000"

EVENT_TYPES = ("payment.failed", "subscription.pending", "order.abandoned")

# root cause -> the error fields Razorpay puts on a failed payment entity
_RZP_ERROR = {
    "insufficient_funds": {
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "error_reason": "insufficient_funds",
    },
    "expired_card": {
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "customer",
        "error_step": "payment_authentication",
        "error_reason": "card_expired",
    },
    "bank_timeout": {
        "error_code": "GATEWAY_ERROR",
        "error_source": "bank",
        "error_step": "payment_authorization",
        "error_reason": "gateway_technical_error",
    },
    "mandate_cancelled": {
        "error_code": "BAD_REQUEST_ERROR",
        "error_source": "customer",
        "error_step": "payment_authorization",
        "error_reason": "payment_mandate_revoked",
    },
    "abandoned": {
        "error_code": None,
        "error_source": None,
        "error_step": None,
        "error_reason": None,
    },
}

_METHOD_CHOICES = ("card", "upi", "netbanking", "wallet")


def _unix(iso_ts: str) -> int:
    return int(datetime.fromisoformat(iso_ts).timestamp())


def _stable_choice(case_id: str, options) -> str:
    """Deterministic pick from ``options`` keyed on the case id (so the feed adds
    no new randomness of its own)."""
    h = int(hashlib.sha256(f"method:{case_id}".encode()).hexdigest()[:8], 16)
    return options[h % len(options)]


def _payment_method(case: dict) -> str:
    rc = case["root_cause"]
    if rc == "expired_card":
        return "card"
    if rc == "mandate_cancelled":
        return "emandate"
    return _stable_choice(case["case_id"], _METHOD_CHOICES)


def _payment_entity(case: dict, *, failed: bool) -> dict:
    cust = case["customer"]
    err = _RZP_ERROR[case["root_cause"]]
    # When the failure text is messy, the structured error_reason is typically
    # missing / generic too - that is *why* someone had to write free text. So we
    # withhold the clean machine code here and let the diagnoser work the text.
    machine_reason = None if case["reason_is_messy"] else err["error_reason"]
    return {
        "id": case["payment_id"] or ("pay_" + case["case_id"].split("_")[1] + "FAILED"),
        "entity": "payment",
        "amount": case["amount_paise"],
        "currency": case["currency"],
        "status": "failed" if failed else "created",
        "order_id": case["order_id"],
        "invoice_id": case["subscription"]["invoice_id"] if case["subscription"] else None,
        "method": _payment_method(case),
        "captured": False,
        "amount_refunded": 0,
        "description": "Recurring charge" if case["subscription"] else "Payment",
        "email": cust["email"],
        "contact": cust["contact"],
        "notes": {"case_id": case["case_id"]},
        "error_code": err["error_code"],
        "error_description": case["raw_failure_reason"],
        "error_source": err["error_source"],
        "error_step": err["error_step"],
        "error_reason": machine_reason,
        "created_at": _unix(case["failed_at"]),
    }


def _order_entity(case: dict) -> dict:
    return {
        "id": case["order_id"],
        "entity": "order",
        "amount": case["amount_paise"],
        "amount_paid": 0,
        "amount_due": case["amount_paise"],
        "currency": case["currency"],
        "status": "created",
        "attempts": 0,
        "notes": {"case_id": case["case_id"]},
        "created_at": _unix(case["failed_at"]),
    }


def _subscription_entity(case: dict) -> dict:
    sub = case["subscription"]
    return {
        "id": sub["subscription_id"],
        "entity": "subscription",
        "plan_id": sub["plan_id"],
        "status": "pending",  # a charge failed; Razorpay retries before 'halted'
        "current_start": None,
        "current_end": None,
        "charge_at": None,
        "total_count": None,
        "paid_count": sub["charge_attempt"] - 1,
        "customer_id": case["customer"]["customer_id"],
        "has_scheduled_changes": False,
        "notes": {"case_id": case["case_id"]},
        "created_at": _unix(case["failed_at"]),
    }


def _envelope(case: dict, event_type: str, contains, payload: dict) -> dict:
    ts = _unix(case["failed_at"])
    return {
        "entity": "event",
        "account_id": ACCOUNT_ID,
        "event": event_type,
        "contains": list(contains),
        "payload": payload,
        "created_at": ts,
        # not part of a real Razorpay webhook - kept for readability / sorting
        "occurred_at": case["failed_at"],
        "case_id": case["case_id"],
    }


def build_event(case: dict) -> dict:
    """One case -> one webhook-shaped event."""
    kind, rc = case["kind"], case["root_cause"]

    if kind == "payment" and rc == "abandoned":
        return _envelope(
            case, "order.abandoned", ["order"],
            {"order": {"entity": _order_entity(case)}},
        )

    if kind == "subscription":
        return _envelope(
            case, "subscription.pending", ["subscription", "payment"],
            {
                "subscription": {"entity": _subscription_entity(case)},
                "payment": {"entity": _payment_entity(case, failed=True)},
            },
        )

    return _envelope(
        case, "payment.failed", ["payment"],
        {"payment": {"entity": _payment_entity(case, failed=True)}},
    )


def build_events(cases) -> list:
    """All cases -> events, ordered by when the failure happened (then case_id)."""
    events = [build_event(c) for c in cases]
    events.sort(key=lambda e: (e["created_at"], e["case_id"]))
    return events


def load_cases(path: Path = None) -> list:
    path = Path(path or config.CASES_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - run 'make generate' (step 2) first."
        )
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_events(events, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events(path: Path = None) -> list:
    path = Path(path or config.EVENTS_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found - run 'make feed' (step 3) first."
        )
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def replay(path: Path = None, cases=None):
    """Yield events one at a time in time order, as if Razorpay were POSTing them.

    Reads ``data/events.jsonl`` by default; pass ``cases=`` to build and replay
    in memory without touching disk.
    """
    events = build_events(cases) if cases is not None else load_events(path)
    for event in events:
        yield event


def _summary(events) -> dict:
    by_type = Counter(e["event"] for e in events)
    span = ""
    if events:
        span = f'{events[0]["occurred_at"]}  ..  {events[-1]["occurred_at"]}'
    return {"count": len(events), "by_type": dict(by_type), "time_span": span}


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Build the replayable event feed.")
    parser.add_argument("--cases", type=Path, default=config.CASES_FILE)
    parser.add_argument("--out", type=Path, default=config.EVENTS_FILE)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    events = build_events(cases)
    write_events(events, args.out)

    s = _summary(events)
    print(f"\nBuilt {s['count']} events -> {args.out}")
    print(f"time span: {s['time_span']}")
    print("by type:")
    for k, v in sorted(s["by_type"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:<22} {v:>4}")


if __name__ == "__main__":
    main()
