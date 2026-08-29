"""Tests for the step 4 diagnoser."""
import pytest

from dunning import config, diagnose
from dunning.feed import build_event, build_events
from dunning.generate import generate_cases


@pytest.fixture(autouse=True)
def _force_fake_llm(monkeypatch):
    """The LLM fallback should resolve via the offline heuristic in tests."""
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")


def _payment_event(*, error_reason=None, error_description="", event_type="payment.failed"):
    return {
        "event": event_type,
        "case_id": "case_test",
        "payload": {"payment": {"entity": {
            "error_reason": error_reason,
            "error_description": error_description,
        }}},
    }


# --- stage-by-stage --------------------------------------------------------- #

def test_abandoned_event_needs_no_llm():
    dx = diagnose.diagnose({"event": "order.abandoned", "case_id": "x", "payload": {"order": {"entity": {}}}})
    assert dx.root_cause == "abandoned"
    assert dx.stage == "event" and dx.confidence == 1.0


@pytest.mark.parametrize("reason,expected", [
    ("insufficient_funds", "insufficient_funds"),
    ("card_expired", "expired_card"),
    ("gateway_technical_error", "bank_timeout"),
    ("payment_mandate_revoked", "mandate_cancelled"),
])
def test_error_reason_lookup(reason, expected):
    dx = diagnose.diagnose(_payment_event(error_reason=reason))
    assert dx.root_cause == expected and dx.stage == "error_reason"


def test_error_reason_beats_misleading_text():
    dx = diagnose.diagnose(_payment_event(
        error_reason="insufficient_funds",
        error_description="looks like the card expired maybe",
    ))
    assert dx.root_cause == "insufficient_funds" and dx.stage == "error_reason"


@pytest.mark.parametrize("text,expected", [
    ("resp code 54 expired card, pls ask cust to update", "expired_card"),
    ("UPI timeout @ NPCI, RRN not generated", "bank_timeout"),
    ("txn declnd - insuff bal, salary on 1st", "insufficient_funds"),
    ("sub charge fail: mandate status = REVOKED", "mandate_cancelled"),
    ("order stuck in 'created', 0 payment attempts logged", "abandoned"),
])
def test_literal_text_rules(text, expected):
    dx = diagnose.diagnose(_payment_event(error_description=text))
    assert dx.root_cause == expected and dx.stage == "text_rules"


@pytest.mark.parametrize("text,expected", [
    ("customer told us the paycheck is late this month", "insufficient_funds"),
    ("the card on file is too old now, issuer won't take it", "expired_card"),
    ("issuer bank was flaky just now, seeing a lot of these", "bank_timeout"),
    ("they turned off auto-pay for us, charge won't go now", "mandate_cancelled"),
    ("customer walked away before paying", "abandoned"),
])
def test_semantic_text_goes_to_llm(text, expected):
    dx = diagnose.diagnose(_payment_event(error_description=text))
    assert dx.root_cause == expected
    assert dx.stage == "llm_fake"


def test_unrecognised_text_is_unknown():
    dx = diagnose.diagnose(_payment_event(error_description="something odd we can't place"))
    assert dx.root_cause == "unknown"


def test_no_signal_at_all_is_unknown():
    dx = diagnose.diagnose(_payment_event())
    assert dx.root_cause == "unknown" and dx.stage == "none"


# --- batch behaviour ------------------------------------------------------ #

def _batch(n=300, seed=42):
    cases = generate_cases(n, seed=seed)
    events = build_events(cases)
    return cases, events


def test_every_prediction_is_in_vocab_and_bounded():
    _, events = _batch()
    for _e, dx in diagnose.diagnose_batch(events):
        assert dx.root_cause in config.ROOT_CAUSES
        assert 0.0 <= dx.confidence <= 1.0


def test_batch_accuracy_offline():
    cases, events = _batch()
    cases_by_id = {c["case_id"]: c for c in cases}
    report = diagnose.score(diagnose.diagnose_batch(events), cases_by_id)
    # offline heuristic; a real Gemini key does better
    assert report["accuracy"] >= 0.92
    assert report["by_stage"].get("error_reason", 0) > 100
    # the LLM fallback is actually exercised
    assert report["by_stage"].get("llm_fake", 0) >= 10


def test_diagnose_is_deterministic():
    _, events = _batch(120)
    assert diagnose.diagnose_batch(events) == diagnose.diagnose_batch(events)


def test_clean_cases_never_touch_the_llm():
    cases, events = _batch()
    by_id = {c["case_id"]: c for c in cases}
    for e, dx in diagnose.diagnose_batch(events):
        if not by_id[e["case_id"]]["reason_is_messy"]:
            assert dx.stage in ("event", "error_reason")


def test_cli_runs(tmp_path, capsys):
    from dunning.feed import main as feed_main
    from dunning.generate import main as gen_main

    cases_p = tmp_path / "cases.jsonl"
    events_p = tmp_path / "events.jsonl"
    dump_p = tmp_path / "dx.jsonl"
    gen_main(["--count", "80", "--seed", "3", "--out", str(cases_p)])
    feed_main(["--cases", str(cases_p), "--out", str(events_p)])

    diagnose.main(["--events", str(events_p), "--cases", str(cases_p), "--dump", str(dump_p)])

    out = capsys.readouterr().out
    assert "Diagnoser accuracy:" in out
    assert len(dump_p.read_text().strip().splitlines()) == 80
