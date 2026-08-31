"""Tests for the step 7 guardrail layer."""
from datetime import datetime, timedelta, timezone

import pytest

from dunning import config, guardrails
from dunning.execute import Attempt, ExecutionResult, run_case
from dunning.feed import build_events
from dunning.generate import generate_cases

G = config.GUARDRAILS
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


# --- GuardrailLedger.evaluate ------------------------------------------- #

def test_first_retry_is_allowed_as_is():
    d = guardrails.GuardrailLedger().evaluate("retry_later", T0)
    assert d.kind == "allow" and d.at == T0 and d.rule == "clear"


def test_second_retry_is_deferred_to_24h_gap():
    led = guardrails.GuardrailLedger()
    led.evaluate("retry_later", T0)
    led.record("retry_later", T0)
    d = led.evaluate("retry_later", T0 + timedelta(hours=3))
    assert d.kind == "defer" and d.rule == "retry_spacing"
    assert d.at == T0 + timedelta(hours=G.min_hours_between_attempts)


def test_retry_cap_skips():
    led = guardrails.GuardrailLedger()
    for i in range(G.max_retries):
        t = T0 + timedelta(hours=48 * i)
        led.evaluate("retry_later", t)
        led.record("retry_later", t)
    d = led.evaluate("retry_later", T0 + timedelta(days=30))
    assert d.kind == "skip" and d.rule == "retry_cap"


def test_message_at_night_is_deferred_into_window():
    night = datetime(2026, 6, 1, 22, 30, tzinfo=timezone.utc)
    d = guardrails.GuardrailLedger().evaluate("send_payment_link", night)
    assert d.kind == "defer" and d.rule == "contact_window"
    assert d.at.hour == G.contact_window_start_hour and d.at.day == 2


def test_message_in_window_is_allowed():
    d = guardrails.GuardrailLedger().evaluate("send_reminder", T0)
    assert d.kind == "allow"


def test_message_cap_skips():
    led = guardrails.GuardrailLedger()
    for _ in range(G.max_messages_per_customer):
        led.evaluate("send_reminder", T0)
        led.record("send_reminder", T0)
    assert led.evaluate("send_payment_link", T0).rule == "message_cap"


def test_hard_action_cap_halts():
    led = guardrails.GuardrailLedger()
    for _ in range(G.max_attempts_hard_cap):
        led.record("handoff_human", T0)
    assert led.evaluate("retry_now", T0).kind == "halt"


def test_every_decision_is_recorded():
    led = guardrails.GuardrailLedger()
    led.evaluate("retry_now", T0)
    led.evaluate("send_reminder", T0)
    assert len(led.decisions) == 2


# --- SpendGovernor ---------------------------------------------------- #

def test_spend_governor_ceiling():
    gov = guardrails.SpendGovernor()
    assert gov.may_attempt(config.MONEY.max_total_attempted_paise)
    gov.note_attempt(config.MONEY.max_total_attempted_paise)
    assert not gov.may_attempt(1)


def test_spend_governor_circuit_breaker():
    gov = guardrails.SpendGovernor()
    for _ in range(config.MONEY.max_gateway_errors):
        assert not gov.tripped()
        gov.note_gateway_error()
    assert gov.tripped()


# --- post-hoc violation check --------------------------------------- #

def _attempt(action, at, outcome):
    return Attempt(0, action, at, at, outcome=outcome)


def test_clean_result_has_no_violations():
    atts = [
        _attempt("retry_later", T0, "failed"),
        _attempt("retry_later", T0 + timedelta(hours=25), "failed"),
        _attempt("send_payment_link", T0 + timedelta(hours=26), "sent"),
        _attempt("handoff_human", T0 + timedelta(hours=27), "escalated"),
    ]
    r = ExecutionResult("c", "x", False, False, 0, "escalated_to_human", 2, 1, atts, T0)
    assert guardrails.count_violations([r]) == 0
    guardrails.assert_no_violations([r])


def test_too_close_retries_are_flagged():
    atts = [
        _attempt("retry_later", T0, "failed"),
        _attempt("retry_later", T0 + timedelta(hours=2), "failed"),
    ]
    r = ExecutionResult("c", "x", False, False, 0, "escalated_to_human", 2, 0, atts, T0)
    assert guardrails.count_violations([r]) >= 1
    with pytest.raises(AssertionError):
        guardrails.assert_no_violations([r])


def test_skipped_and_replanned_rows_do_not_count():
    atts = [
        _attempt("send_reminder", T0, "replanned"),
        _attempt("send_mandate_link", T0.replace(hour=9), "sent"),
        _attempt("send_mandate_link", (T0 + timedelta(days=2)).replace(hour=9), "sent"),
        _attempt("send_mandate_link", (T0 + timedelta(days=3)).replace(hour=9), "skipped"),
    ]
    r = ExecutionResult("c", "mandate_cancelled", True, False, 0, "escalated_to_human",
                        0, 2, atts, T0)
    assert guardrails.count_violations([r]) == 0


# --- integration ---------------------------------------------------- #

def test_real_batch_has_zero_violations():
    cases = generate_cases(200, seed=42)
    by_id = {c["case_id"]: c for c in cases}
    from dunning.execute import RazorpayGateway
    gw = RazorpayGateway(None, live=False)
    results = [run_case(by_id[e["case_id"]], e, seed=42, gateway=gw)
              for e in build_events(cases)]
    guardrails.assert_no_violations(results)
    assert guardrails.count_violations(results) == 0
