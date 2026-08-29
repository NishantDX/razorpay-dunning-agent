"""Tests for the step 5 policy engine."""
from datetime import datetime, timezone

import pytest

from dunning import config, diagnose, policy
from dunning.feed import build_events
from dunning.generate import generate_cases
from dunning.policy import Context, plan


def _actions(p):
    return [s.action for s in p.steps]


# --- YAML integrity ------------------------------------------------------- #

def test_yaml_causes_match_config_vocab():
    assert set(policy._POLICY["causes"]) == set(config.ROOT_CAUSES)


def test_yaml_templates_only_use_real_interventions():
    for name, steps in policy._POLICY["templates"].items():
        for raw in steps:
            assert raw["action"] in config.INTERVENTIONS, (name, raw)


# --- every cause plans, and stays inside the vocab + caps ---------------- #

@pytest.mark.parametrize("cause", config.ROOT_CAUSES)
def test_every_cause_produces_a_valid_plan(cause):
    p = plan(cause, Context(amount_rupees=2000))
    assert p.steps, cause
    assert all(s.action in config.INTERVENTIONS for s in p.steps)
    assert _actions(p)[-1] in ("handoff_human", "do_nothing")


@pytest.mark.parametrize("cause", config.ROOT_CAUSES)
def test_plan_never_exceeds_guardrails(cause):
    g = config.GUARDRAILS
    for ctx in (Context(amount_rupees=2000),
                Context(amount_rupees=2000, reachable=False),
                Context(amount_rupees=50, ),
                Context(amount_rupees=45000),
                Context(is_subscription=True, mandate_active=False, amount_rupees=800)):
        p = plan(cause, ctx)
        retries = sum(a in config.RETRY_INTERVENTIONS for a in _actions(p))
        messages = sum(a in config.MESSAGE_INTERVENTIONS for a in _actions(p))
        assert retries <= g.max_retries, (cause, ctx, _actions(p))
        assert messages <= g.max_messages_per_customer, (cause, ctx, _actions(p))


# --- cause-specific guarantees ----------------------------------------- #

@pytest.mark.parametrize("cause", sorted(config.NEVER_RETRY_CAUSES))
def test_never_retry_causes_have_zero_retry_steps(cause):
    p = plan(cause, Context(amount_rupees=2000))
    assert not any(a in config.RETRY_INTERVENTIONS for a in _actions(p)), _actions(p)


@pytest.mark.parametrize("cause", sorted(config.RETRY_SAFE_CAUSES))
def test_retry_safe_causes_include_a_retry(cause):
    p = plan(cause, Context(amount_rupees=2000))
    assert any(a in config.RETRY_INTERVENTIONS for a in _actions(p)), _actions(p)


def test_mandate_cancelled_is_mandate_link_first():
    p = plan("mandate_cancelled", Context(is_subscription=True, mandate_active=False,
                                          amount_rupees=2000))
    assert _actions(p)[0] == "send_mandate_link"
    assert "retry_now" not in _actions(p) and "retry_later" not in _actions(p)


def test_abandoned_starts_with_a_payment_link():
    assert _actions(plan("abandoned", Context(amount_rupees=2000)))[0] == "send_payment_link"


def test_needs_review_is_human_only():
    assert _actions(plan("needs_review", Context(amount_rupees=2000))) == ["handoff_human"]


def test_stolen_card_never_re_plans():
    p = plan("stolen_or_lost_card", Context(amount_rupees=2000))
    assert p.replan_allowed is False
    assert _actions(p) == ["handoff_human"]


# --- context adjustments --------------------------------------------------- #

def test_dead_mandate_overrides_any_cause():
    p = plan("insufficient_funds", Context(is_subscription=True, mandate_active=False,
                                           amount_rupees=2000))
    assert _actions(p)[0] == "send_mandate_link"
    assert "dead_mandate_override" in p.adjustments
    assert not any(a in config.RETRY_INTERVENTIONS for a in _actions(p))


def test_unreachable_customer_gets_no_message_steps():
    for cause in ("expired_card", "insufficient_funds", "do_not_honour", "abandoned"):
        p = plan(cause, Context(amount_rupees=2000, reachable=False))
        assert not any(a in config.MESSAGE_INTERVENTIONS for a in _actions(p)), (cause, _actions(p))


def test_high_value_risk_block_goes_straight_to_human():
    p = plan("card_declined_risk", Context(amount_rupees=45000))
    assert _actions(p) == ["handoff_human"]
    assert p.replan_allowed is False
    assert "high_value_risk_escalation" in p.adjustments


def test_low_value_is_written_off_not_escalated():
    p = plan("expired_card", Context(amount_rupees=90))
    assert _actions(p)[-1] == "do_nothing"
    assert "handoff_human" not in _actions(p)
    assert "low_value_write_off" in p.adjustments


def test_plan_is_deterministic():
    ctx = Context(amount_rupees=1234, reachable=False, is_subscription=True, mandate_active=True)
    assert plan("do_not_honour", ctx) == plan("do_not_honour", ctx)


# --- scheduling --------------------------------------------------------- #

def test_schedule_is_monotonic_and_resolves_month_start():
    t0 = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)
    p = plan("insufficient_funds", Context(amount_rupees=2000))
    timeline = policy.schedule(p, t0)
    times = [t for _s, t in timeline]
    assert times == sorted(times)
    # the month_start retry lands on 1 April
    month_start_steps = [t for s, t in timeline if s.wait.at == "month_start"]
    assert month_start_steps and month_start_steps[0].month == 4
    assert month_start_steps[0].day == 1


def test_schedule_month_start_wraps_year():
    t0 = datetime(2026, 12, 20, 9, 0, tzinfo=timezone.utc)
    assert policy._next_month_start(t0) == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


# --- batch smoke ------------------------------------------------------- #

def test_plans_the_whole_batch_without_error():
    cases = generate_cases(300, seed=42)
    by_id = {c["case_id"]: c for c in cases}
    for event in build_events(cases):
        case = by_id[event["case_id"]]
        dx = diagnose.diagnose(event)
        p = plan(dx.root_cause, policy.context_from_case(case))
        assert p.steps
        assert all(s.action in config.INTERVENTIONS for s in p.steps)


def test_cli_runs(tmp_path):
    from dunning.feed import main as feed_main
    from dunning.generate import main as gen_main

    cases_p = tmp_path / "cases.jsonl"
    events_p = tmp_path / "events.jsonl"
    dump_p = tmp_path / "plans.jsonl"
    gen_main(["--count", "60", "--seed", "5", "--out", str(cases_p)])
    feed_main(["--cases", str(cases_p), "--out", str(events_p)])
    policy.main(["--events", str(events_p), "--cases", str(cases_p), "--dump", str(dump_p)])
    assert len(dump_p.read_text().strip().splitlines()) == 60
