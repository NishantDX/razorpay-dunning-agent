"""Tests for the step 6 executor. Uses the local fake gateway and the offline
LLM; no network."""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from dunning import config, execute, policy
from dunning.execute import Clock, RazorpayGateway, execute_plan, run_case
from dunning.feed import build_events
from dunning.generate import generate_cases

G = config.GUARDRAILS
_ZERO_LATENT = {
    "base_recovery_prob": 0.0, "funds_return_day": None, "timing_bonus_prob": 0.0,
    "transient_retry_prob": 0.0, "limit_resets": False, "method_dead": False,
    "link_response_prob": 0.0, "mandate_link_prob": 0.0, "opt_out_prob": 0.0,
    "chronic": False, "mandate_revokes_at_attempt": None,
}


def _case(root_cause="insufficient_funds", *, kind="payment", amount=2000,
          reachable=True, failed_at="2026-08-10T10:00:00+05:30",
          mandate_status="active", **latent):
    L = dict(_ZERO_LATENT, **latent)
    return {
        "case_id": "case_test", "kind": kind, "amount_paise": amount * 100,
        "amount_rupees": amount, "currency": "INR", "failed_at": failed_at,
        "root_cause": root_cause,
        "customer": {"name": "Test User", "email": "t@example.com",
                     "contact": "+919000000000", "language": "en",
                     "reachable": reachable, "prior_payments": 3,
                     "prior_success_rate": 0.9},
        "subscription": ({"subscription_id": "sub_x", "mandate_status": mandate_status,
                          "invoice_id": "inv_x", "recurring_amount_paise": amount * 100}
                         if kind == "subscription" else None),
        "latent": L,
    }


def _gw():
    return RazorpayGateway(None, live=False)


def _run(case, **plan_over):
    p = policy.plan(case["root_cause"], policy.context_from_case(case))
    if plan_over:
        p = dataclasses.replace(p, **plan_over)
    return execute_plan(case, p, seed=1, gateway=_gw())


# --- clock + gateway ---------------------------------------------------- #

def test_clock_only_moves_forward():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    c = Clock(t)
    c.advance_to(t - timedelta(hours=5))
    assert c.now() == t
    c.advance_to(t + timedelta(hours=5))
    assert c.now() == t + timedelta(hours=5)


def test_gateway_idempotency_blocks_double_charge():
    gw = _gw()
    a = gw.create_order(5000, {"case_id": "c"}, "k1")
    b = gw.create_order(5000, {"case_id": "c"}, "k1")
    assert a == b
    assert gw.calls.count(("create_order", "k1")) == 1


def test_gateway_falls_back_to_fake_without_keys(monkeypatch):
    monkeypatch.setattr(config, "RAZORPAY_DRY_RUN", True)
    assert execute.make_gateway().live is False


@pytest.mark.parametrize("day,dom,near", [
    (1, 1, True), (1, 2, True), (1, 15, False), (30, 1, True), (1, 31, True), (15, 20, False),
])
def test_near_day(day, dom, near):
    dt = datetime(2026, 1, dom, 12, 0, tzinfo=timezone.utc)  # January: 31 days
    assert execute._near_day(dt, day) is near


# --- outcomes --------------------------------------------------------- #

def test_recovers_on_first_retry_when_latent_certain():
    r = _run(_case("bank_timeout", base_recovery_prob=1.0, transient_retry_prob=1.0))
    assert r.recovered and r.stop_reason == "recovered"
    assert r.amount_recovered_paise == 200000
    assert r.retries_used == 1


def test_dead_card_recovers_only_via_link():
    r = _run(_case("expired_card", method_dead=True, link_response_prob=1.0))
    assert r.recovered and r.stop_reason == "recovered"
    assert [a.action for a in r.attempts][0] == "send_payment_link"
    assert r.retries_used == 0


def test_hopeless_case_escalates_with_nothing_recovered():
    r = _run(_case("do_not_honour", chronic=True))
    assert not r.recovered
    assert r.stop_reason == "escalated_to_human"
    assert r.amount_recovered_paise == 0
    assert r.attempts[-1].action == "handoff_human"


def test_low_value_is_written_off():
    r = _run(_case("expired_card", amount=90, link_response_prob=0.0))
    assert r.stop_reason == "written_off"
    assert r.attempts[-1].action == "do_nothing"


def test_customer_opt_out_stops_the_sequence():
    r = _run(_case("expired_card", link_response_prob=1.0, opt_out_prob=1.0))
    assert r.stop_reason == "customer_opted_out" and not r.recovered


# --- guardrails ------------------------------------------------------- #

def test_retries_are_spaced_at_least_24h():
    r = _run(_case("insufficient_funds"))  # zero latent -> every retry fails
    retry_times = [a.at for a in r.attempts if a.action in config.RETRY_INTERVENTIONS]
    assert len(retry_times) >= 2
    for earlier, later in zip(retry_times, retry_times[1:]):
        assert later - earlier >= timedelta(hours=G.min_hours_between_attempts)


def test_messages_are_moved_into_the_contact_window():
    r = _run(_case("insufficient_funds", failed_at="2026-08-10T22:30:00+05:30"))
    msgs = [a for a in r.attempts if a.action in config.MESSAGE_INTERVENTIONS]
    assert msgs
    for a in msgs:
        assert G.contact_window_start_hour <= a.at.hour < G.contact_window_end_hour


@pytest.mark.parametrize("cause", list(config.ROOT_CAUSES))
def test_never_exceeds_hard_caps(cause):
    for ctx_kw in ({}, {"reachable": False}, {"amount": 80}, {"amount": 40000}):
        r = _run(_case(cause, **ctx_kw))
        assert r.retries_used <= G.max_retries
        assert r.messages_sent <= G.max_messages_per_customer
        assert len(r.attempts) <= G.max_attempts_hard_cap + 1


# --- re-planning ------------------------------------------------------ #

def test_mandate_revoke_triggers_one_replan():
    case = _case("insufficient_funds", kind="subscription",
                 mandate_revokes_at_attempt=1, mandate_link_prob=0.0)
    r = _run(case)
    assert r.replanned is True
    assert r.root_cause == "mandate_cancelled"
    assert any(a.outcome == "replanned" for a in r.attempts)
    # after the re-plan it works the mandate, then hands off
    assert r.stop_reason in ("escalated_to_human", "recovered", "mandate_dead")
    assert not any(a.action in config.RETRY_INTERVENTIONS for a in r.attempts)


def test_mandate_revoke_stops_safely_when_replan_disabled():
    case = _case("bank_timeout", kind="subscription", mandate_revokes_at_attempt=2)
    r = _run(case, replan_allowed=False)
    assert r.stop_reason == "mandate_dead" and not r.recovered
    assert r.attempts[-1].outcome == "mandate_dead"


# --- determinism + batch -------------------------------------------- #

def test_execution_is_deterministic():
    case = _case("do_not_honour", link_response_prob=0.5, opt_out_prob=0.1)
    a = execute.result_to_dict(_run(case))
    b = execute.result_to_dict(_run(case))
    assert a == b


def test_batch_runs_clean():
    cases = generate_cases(150, seed=42)
    by_id = {c["case_id"]: c for c in cases}
    gw = _gw()
    results = []
    at_risk = recovered_val = 0
    for event in build_events(cases):
        case = by_id[event["case_id"]]
        r = run_case(case, event, seed=42, gateway=gw)
        results.append(r)
        at_risk += case["amount_paise"]
        if r.recovered:
            assert r.amount_recovered_paise == case["amount_paise"]
            recovered_val += r.amount_recovered_paise
        assert r.stop_reason in config.STOP_REASONS
    assert recovered_val <= at_risk
    assert execute._violations(results) == 0


def test_cli_runs(tmp_path):
    from dunning.feed import main as feed_main
    from dunning.generate import main as gen_main

    cases_p = tmp_path / "cases.jsonl"
    events_p = tmp_path / "events.jsonl"
    dump_p = tmp_path / "runs.jsonl"
    gen_main(["--count", "80", "--seed", "2", "--out", str(cases_p)])
    feed_main(["--cases", str(cases_p), "--out", str(events_p)])
    execute.main(["--events", str(events_p), "--cases", str(cases_p), "--dump", str(dump_p)])
    lines = dump_p.read_text().strip().splitlines()
    assert len(lines) == 80
