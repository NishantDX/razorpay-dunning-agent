"""Step 4 - the diagnoser.

Turns one event (a Razorpay-webhook-shaped dict from the feed) into one canonical
root cause from ``config.ROOT_CAUSES``. A three-stage cascade, cheapest first:

1. **event type**   - an ``order.abandoned`` event *is* the diagnosis.
2. **error_reason**  - Razorpay's structured machine code
   (``insufficient_funds``, ``card_expired``, ...). A lookup table. No AI.
3. **error_description text** - a small, deliberately *literal* rules table
   (matches "expired", "timeout", "insuff bal", "504", ...). No AI.
4. **LLM fallback**  - only text that stages 2-3 could not place (the messy
   free-text ~15%) goes to ``llm.classify_failure``. This is the single spot in
   the whole agent where an LLM touches a money decision, and it only ever
   *labels* - the policy engine (step 5) decides what to do.

``diagnose(event) -> Diagnosis`` carries the label, a confidence, which stage
decided it, and the exact signal it keyed on - so every diagnosis is auditable.

CLI:  python -m dunning.diagnose   # scores the whole feed against ground truth
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dunning import config, feed, llm

# --------------------------------------------------------------------------- #
# Stage 2: Razorpay structured error_reason -> our vocabulary
# --------------------------------------------------------------------------- #

_REASON_MAP = {
    # funds / limits
    "insufficient_funds": "insufficient_funds",
    "payment_limit_exceeded": "card_limit_exceeded",
    "card_limit_exceeded": "card_limit_exceeded",
    # transient infrastructure
    "gateway_timeout": "bank_timeout",
    "request_timeout": "bank_timeout",
    "issuer_not_available": "issuer_unavailable",
    "bank_not_available": "issuer_unavailable",
    "server_error": "technical_decline",
    "gateway_technical_error": "technical_decline",
    # authentication
    "authentication_failed": "three_ds_failed",
    "3ds_authentication_failed": "three_ds_failed",
    # instrument unusable
    "card_expired": "expired_card",
    "expired_card": "expired_card",
    "invalid_card_details": "invalid_payment_details",
    "invalid_card_number": "invalid_payment_details",
    "incorrect_card_details": "invalid_payment_details",
    "international_not_allowed": "international_blocked",
    "international_transaction_not_allowed": "international_blocked",
    # issuer refusal
    "payment_declined_by_bank": "do_not_honour",
    "do_not_honour": "do_not_honour",
    # security
    "suspected_fraud": "card_declined_risk",
    "fraudulent_payment": "card_declined_risk",
    "card_reported_lost_or_stolen": "stolen_or_lost_card",
    "card_reported_stolen": "stolen_or_lost_card",
    "card_reported_lost": "stolen_or_lost_card",
    # mandate
    "payment_mandate_revoked": "mandate_cancelled",
    "mandate_revoked": "mandate_cancelled",
}

# --------------------------------------------------------------------------- #
# Stage 3: literal text rules. High precision only - anything that needs
# interpretation is left for the LLM on purpose. Checked in this order (the
# more specific / dangerous causes first).
# --------------------------------------------------------------------------- #

_TEXT_RULES = [
    ("stolen_or_lost_card", re.compile(
        r"reported (?:lost|stolen)|\bstolen\b|lost or stolen|resp(?:onse)? ?4[13]\b|pick ?up card", re.I)),
    ("card_declined_risk", re.compile(
        r"suspected fraud|\bfraud\b|risk (?:block|engine|flag)|security (?:reason|violation)|resp(?:onse)? ?59\b", re.I)),
    ("mandate_cancelled", re.compile(
        r"\bmandate\b|e-?nach|auto-?debit|\bautopay\b|token rejected|si (?:cancel|revok)", re.I)),
    ("three_ds_failed", re.compile(
        r"3-?d-?s(?:ecure)?|\b3ds\b|\bafa\b|\botp\b|authentication (?:failed|not completed)", re.I)),
    ("international_blocked", re.compile(
        r"international|cross-?border|overseas card|foreign card|card('?s)? country", re.I)),
    ("expired_card", re.compile(
        r"\bexpired\b|\bexpiry\b|exp(?:iry)? ?(?:date|\d)|resp(?:onse)? ?54\b", re.I)),
    ("invalid_payment_details", re.compile(
        r"invalid card (?:number|details)|incorrect card|bad cvv|resp(?:onse)? ?14\b", re.I)),
    ("card_limit_exceeded", re.compile(
        r"limit (?:exceeded|reached)|exceeds .{0,20}limit|withdrawal (?:limit|frequency)|resp(?:onse)? ?6[15]\b", re.I)),
    ("issuer_unavailable", re.compile(
        r"issuer (?:down|not available|unavailable|inoperative)|bank not available|resp(?:onse)? ?91\b", re.I)),
    ("abandoned", re.compile(
        r"\babandon|did ?n[o']t complete|0 payment attempts|no payment attempts? (?:logged|on)", re.I)),
    ("bank_timeout", re.compile(
        r"time ?d? ?out|\btimeout\b|no response|did not respond|\b50[24]\b|socket closed|upstream", re.I)),
    ("do_not_honour", re.compile(
        r"do not honou?r|\bdnh\b|resp(?:onse)? ?05\b|declined by (?:the )?issuing bank", re.I)),
    ("technical_decline", re.compile(
        r"technical (?:error|decline)|gateway_error|processor (?:error|technical)", re.I)),
    ("insufficient_funds", re.compile(
        r"insuffic|insuff bal|\bnsf\b|low funds|low bal\b|balance too low"
        r"|reason code:? .{0,3}insufficient", re.I)),
]

# below this, an LLM label is treated as "not confident enough" -> needs_review
_LLM_MIN_CONFIDENCE = 0.4


@dataclass(frozen=True)
class Diagnosis:
    root_cause: str      # one of config.ROOT_CAUSES
    confidence: float
    stage: str           # "event" | "error_reason" | "text_rules" | "llm" | "llm_cache" | "llm_fake" | "none"
    signal: str          # the exact thing it matched on


def _payment_entity(event: dict) -> dict:
    payload = event.get("payload", {})
    return (payload.get("payment") or {}).get("entity") or {}


def _match_text(text: str):
    for label, pattern in _TEXT_RULES:
        m = pattern.search(text)
        if m:
            return label, m.group(0)
    return None, None


def diagnose(event: dict) -> Diagnosis:
    if event.get("event") == "order.abandoned":
        return Diagnosis("abandoned", 1.0, "event", "order.abandoned")

    ent = _payment_entity(event)

    reason = (ent.get("error_reason") or "").strip().lower()
    if reason in _REASON_MAP:
        return Diagnosis(_REASON_MAP[reason], 0.95, "error_reason", f"error_reason={reason}")

    text = (ent.get("error_description") or "").strip()
    if text:
        label, matched = _match_text(text)
        if label:
            return Diagnosis(label, 0.8, "text_rules", f"~{matched!r}")

        res = llm.classify_failure(text, choices=config.ROOT_CAUSES)
        stage = "llm_fake" if res.model == "fake" else ("llm_cache" if res.cached else "llm")
        if res.label in ("needs_review", "unknown") or res.confidence < _LLM_MIN_CONFIDENCE:
            return Diagnosis("needs_review", res.confidence, stage, f"{res.model}:{res.label}")
        return Diagnosis(res.label, res.confidence, stage, f"{res.model}:{res.label}")

    return Diagnosis("needs_review", 0.0, "none", "no error_reason or text")


def diagnose_batch(events) -> list:
    return [(e, diagnose(e)) for e in events]


# --------------------------------------------------------------------------- #
# Scoring against ground truth (cases carry the true root_cause)
# --------------------------------------------------------------------------- #

def score(pairs, cases_by_id: dict) -> dict:
    total = len(pairs)
    correct = 0
    by_stage = Counter()
    by_stage_correct = Counter()
    confusion = Counter()   # (true, predicted) when wrong
    misses = []

    for event, dx in pairs:
        truth = cases_by_id[event["case_id"]]["root_cause"]
        by_stage[dx.stage] += 1
        ok = dx.root_cause == truth
        if ok:
            correct += 1
            by_stage_correct[dx.stage] += 1
        else:
            confusion[(truth, dx.root_cause)] += 1
            misses.append((event["case_id"], truth, dx.root_cause, dx.stage, dx.signal))

    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "by_stage": dict(by_stage),
        "by_stage_accuracy": {
            s: by_stage_correct[s] / by_stage[s] for s in by_stage
        },
        "confusion": {f"{t} -> {p}": n for (t, p), n in confusion.most_common()},
        "misses": misses,
    }


def _print_report(report: dict) -> None:
    r = report
    print(f"\nDiagnoser accuracy: {r['correct']}/{r['total']}  ({r['accuracy']:.1%})")
    print("\n  by stage (count, accuracy):")
    for stage, n in sorted(r["by_stage"].items(), key=lambda kv: -kv[1]):
        print(f"    {stage:<12} {n:>4}   {r['by_stage_accuracy'][stage]:.1%}")
    if r["confusion"]:
        print("\n  misclassifications (true -> predicted):")
        for k, n in r["confusion"].items():
            print(f"    {k:<40} {n:>3}")
    n_show = min(15, len(r["misses"]))
    if n_show:
        print(f"\n  first {n_show} misses:")
        for cid, truth, pred, stage, sig in r["misses"][:n_show]:
            print(f"    {cid}  {truth:>18} -> {pred:<18} [{stage}] {sig}")
    print()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Diagnose the feed and score it.")
    parser.add_argument("--events", type=Path, default=config.EVENTS_FILE)
    parser.add_argument("--cases", type=Path, default=config.CASES_FILE)
    parser.add_argument("--dump", type=Path, default=None,
                        help="also write per-event diagnoses to this JSONL path")
    args = parser.parse_args(argv)

    events = feed.load_events(args.events)
    cases_by_id = {c["case_id"]: c for c in feed.load_cases(args.cases)}
    pairs = diagnose_batch(events)

    if args.dump:
        with Path(args.dump).open("w", encoding="utf-8") as fh:
            for event, dx in pairs:
                fh.write(json.dumps({
                    "case_id": event["case_id"],
                    "event": event["event"],
                    "root_cause": dx.root_cause,
                    "confidence": dx.confidence,
                    "stage": dx.stage,
                    "signal": dx.signal,
                }, ensure_ascii=False) + "\n")

    report = score(pairs, cases_by_id)
    _print_report(report)
    print(f"LLM provider in use: {llm.active_provider()}"
          f"  (set GEMINI_API_KEY for the real classifier)\n")


if __name__ == "__main__":
    main()
