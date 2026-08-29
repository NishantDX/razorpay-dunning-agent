"""Step 5 - the policy engine.

``plan(root_cause, context) -> Plan``: given a diagnosed root cause and the
customer / subscription situation, produce an ordered list of recovery steps,
each with a minimum wait. A pure deterministic function - no LLM, no I/O beyond
reading ``config/policy.yaml`` once at import.

Division of labour:
* ``config/policy.yaml`` - the cause -> steps spine, auditable at a glance.
* this module - the context-sensitive adjustments (unreachable customer, dead
  mandate, low-value write-off, high-value risk escalation), which are branch
  logic rather than a table.

The plan is fixed up front, so it is fully auditable and testable. At run time
the executor may (a) cut it short on a stopping rule, and (b) ask for ONE
re-plan if a later attempt fails with a materially different cause
(``replan_allowed``). Anything more adaptive than that belongs to the guardrails
(step 7), not here.

CLI:  python -m dunning.policy   # plan every event in the feed, summarised
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from dunning import config, diagnose, feed

# Below this value a manual touch costs more than the money at stake - write off.
LOW_VALUE_RUPEES = 150
# A risk-blocked charge at least this large skips straight to a human.
HIGH_VALUE_RUPEES = 10_000

# Causes where we never re-engage automatically even if a later attempt looks
# different - the first read is treated as final.
_NEVER_REPLAN = frozenset({"stolen_or_lost_card", "card_declined_risk", "needs_review"})

_POLICY = yaml.safe_load(Path(config.POLICY_FILE).read_text("utf-8"))


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Wait:
    after_hours: float = 0.0
    at: str = ""              # "" | "month_start"
    within_hours: float = 0.0

    @classmethod
    def from_yaml(cls, raw: dict) -> "Wait":
        return cls(
            after_hours=float(raw.get("after_hours", 0.0)),
            at=str(raw.get("at", "")),
            within_hours=float(raw.get("within_hours", 0.0)),
        )


@dataclass(frozen=True)
class Step:
    action: str               # a config.INTERVENTIONS value
    wait: Wait
    note: str = ""


@dataclass(frozen=True)
class Context:
    is_subscription: bool = False
    mandate_active: bool = True
    reachable: bool = True
    prior_payments: int = 0
    amount_rupees: int = 0
    language: str = "en"


@dataclass(frozen=True)
class Plan:
    root_cause: str            # the diagnosed cause this plan responds to
    steps: tuple               # tuple[Step, ...]
    rationale: str
    replan_allowed: bool
    adjustments: tuple = field(default_factory=tuple)  # context tweaks applied, for audit


# --------------------------------------------------------------------------- #
# Building the base plan from the YAML table
# --------------------------------------------------------------------------- #

def _template_steps(name: str) -> list:
    steps = []
    for raw in _POLICY["templates"][name]:
        steps.append(Step(raw["action"], Wait.from_yaml(raw["wait"])))
    return steps


def _base_plan(root_cause: str):
    entry = _POLICY["causes"].get(root_cause)
    if entry is None:  # unknown cause label -> treat as needs_review
        entry = _POLICY["causes"]["needs_review"]
        root_cause = "needs_review"
    steps = _template_steps(entry["template"])
    rationale = " ".join(entry["rationale"].split())
    return steps, rationale, root_cause


# --------------------------------------------------------------------------- #
# Context-sensitive adjustments
# --------------------------------------------------------------------------- #

def _drop_unreachable_steps(steps: list) -> list:
    """A customer we cannot reach can't receive a reminder or a link. Keep the
    steps that don't need them (retries), turn link steps into a human handoff,
    and collapse the result."""
    out = []
    for step in steps:
        if step.action == "send_reminder":
            continue
        if step.action in ("send_payment_link", "send_mandate_link"):
            step = Step("handoff_human", step.wait,
                        "Customer unreachable - a person makes contact instead.")
        if step.action == "handoff_human" and out and out[-1].action == "handoff_human":
            continue
        out.append(step)
    return out or [Step("handoff_human", Wait(after_hours=1),
                        "Customer unreachable and nothing else applies.")]


def _write_off_tail(steps: list) -> list:
    """Below LOW_VALUE_RUPEES a human handoff isn't worth it - stop after the
    automated attempts."""
    out = []
    for step in steps:
        if step.action == "handoff_human":
            out.append(Step("do_nothing", step.wait,
                            "Value below the cost of manual recovery - written off."))
            break
        out.append(step)
    if not any(s.action == "do_nothing" for s in out):
        out.append(Step("do_nothing", Wait(after_hours=0),
                        "Value below the cost of manual recovery - written off."))
    return out


def _clamp_to_guardrails(steps: list) -> list:
    """Safety net: never hand back a plan that exceeds the hard caps."""
    g = config.GUARDRAILS
    retries = messages = 0
    out = []
    for step in steps:
        if step.action in config.RETRY_INTERVENTIONS:
            if retries >= g.max_retries:
                continue
            retries += 1
        if step.action in config.MESSAGE_INTERVENTIONS:
            if messages >= g.max_messages_per_customer:
                continue
            messages += 1
        out.append(step)
    if not out or out[-1].action not in ("handoff_human", "do_nothing"):
        out.append(Step("handoff_human", Wait(after_hours=24),
                        "Recovery steps exhausted."))
    return out


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def plan(root_cause: str, context: Context = Context()) -> Plan:
    steps, rationale, effective = _base_plan(root_cause)
    adjustments = []

    # 1. A dead subscription mandate blocks everything until it is repaired.
    if context.is_subscription and not context.mandate_active and effective != "mandate_cancelled":
        steps, mandate_rationale, _ = _base_plan("mandate_cancelled")
        rationale = ("Subscription mandate is inactive, so it must be repaired "
                     "before any charge can succeed. " + mandate_rationale)
        adjustments.append("dead_mandate_override")

    # 2. A high-value risk block goes straight to a human - no link, no retry.
    if effective == "card_declined_risk" and context.amount_rupees >= HIGH_VALUE_RUPEES:
        steps = [Step("handoff_human", Wait(after_hours=1),
                      "High-value transaction blocked for risk - immediate human review.")]
        adjustments.append("high_value_risk_escalation")

    # 3. An unreachable customer can't receive messages.
    if not context.reachable:
        before = len(steps)
        steps = _drop_unreachable_steps(steps)
        if len(steps) != before or any(s.note.startswith("Customer unreachable") for s in steps):
            adjustments.append("unreachable_customer")

    # 4. A very small amount isn't worth a person's time.
    if 0 < context.amount_rupees < LOW_VALUE_RUPEES:
        steps = _write_off_tail(steps)
        adjustments.append("low_value_write_off")

    # 5. Hard-cap safety net.
    steps = _clamp_to_guardrails(steps)

    replan = (root_cause not in _NEVER_REPLAN) and ("high_value_risk_escalation" not in adjustments)
    return Plan(root_cause, tuple(steps), rationale, replan, tuple(adjustments))


# --------------------------------------------------------------------------- #
# Scheduling (calendar math only; the >=24h spacing and 09:00-20:00 window are
# enforced by the guardrails in step 7, not here)
# --------------------------------------------------------------------------- #

def _next_month_start(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return dt.replace(month=dt.month + 1, day=1,
                      hour=0, minute=0, second=0, microsecond=0)


def schedule(a_plan: Plan, t0: datetime) -> list:
    """Resolve each step's Wait into an earliest datetime, relative to t0."""
    out = []
    cursor = t0
    for step in a_plan.steps:
        if step.wait.at == "month_start":
            earliest = _next_month_start(cursor)
        else:
            earliest = cursor + timedelta(hours=step.wait.after_hours)
        out.append((step, earliest))
        cursor = earliest
    return out


# --------------------------------------------------------------------------- #
# Case -> context, and the CLI
# --------------------------------------------------------------------------- #

def context_from_case(case: dict) -> Context:
    sub = case.get("subscription")
    return Context(
        is_subscription=case["kind"] == "subscription",
        mandate_active=(sub is None) or (sub.get("mandate_status") == "active"),
        reachable=case["customer"]["reachable"],
        prior_payments=case["customer"]["prior_payments"],
        amount_rupees=case["amount_rupees"],
        language=case["customer"]["language"],
    )


def _print_summary(rows) -> None:
    total = len(rows)
    first_action = Counter(p.steps[0].action for _c, p in rows)
    by_cause = Counter(p.root_cause for _c, p in rows)
    adjustments = Counter(a for _c, p in rows for a in p.adjustments)
    straight_to_human = sum(1 for _c, p in rows if p.steps[0].action == "handoff_human")
    written_off = sum(1 for _c, p in rows if any(s.action == "do_nothing" for s in p.steps))
    replan_off = sum(1 for _c, p in rows if not p.replan_allowed)

    print(f"\nPlanned {total} cases\n")
    print("  first action:")
    for k, v in first_action.most_common():
        print(f"    {k:<18} {v:>4}  ({v / total:.0%})")
    print("\n  plan by diagnosed cause:")
    for k, v in by_cause.most_common():
        print(f"    {k:<24} {v:>4}")
    print("\n  context adjustments applied:")
    for k, v in adjustments.most_common() or [("(none)", 0)]:
        print(f"    {k:<26} {v:>4}")
    print(f"\n  straight to a human : {straight_to_human}")
    print(f"  written off (low value): {written_off}")
    print(f"  re-plan disabled    : {replan_off}\n")

    print("  base plan per cause (reachable customer, mid-value):")
    ctx = Context(amount_rupees=2000)
    for cause in config.ROOT_CAUSES:
        steps = " -> ".join(s.action for s in plan(cause, ctx).steps)
        print(f"    {cause:<24} {steps}")
    print()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Plan a recovery for every event.")
    parser.add_argument("--events", type=Path, default=config.EVENTS_FILE)
    parser.add_argument("--cases", type=Path, default=config.CASES_FILE)
    parser.add_argument("--dump", type=Path, default=None,
                        help="write per-case plans to this JSONL path")
    args = parser.parse_args(argv)

    events = feed.load_events(args.events)
    cases_by_id = {c["case_id"]: c for c in feed.load_cases(args.cases)}

    rows = []
    for event in events:
        case = cases_by_id[event["case_id"]]
        dx = diagnose.diagnose(event)
        p = plan(dx.root_cause, context_from_case(case))
        rows.append((case, p))

    if args.dump:
        with Path(args.dump).open("w", encoding="utf-8") as fh:
            for case, p in rows:
                fh.write(json.dumps({
                    "case_id": case["case_id"],
                    "root_cause": p.root_cause,
                    "steps": [{"action": s.action, "wait": vars(s.wait), "note": s.note}
                              for s in p.steps],
                    "rationale": p.rationale,
                    "replan_allowed": p.replan_allowed,
                    "adjustments": list(p.adjustments),
                }, ensure_ascii=False) + "\n")

    _print_summary(rows)


if __name__ == "__main__":
    main()
