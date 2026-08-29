"""Smoke tests - just enough to prove the package imports and config is sane."""
from dunning import config


def test_guardrails_are_sane():
    g = config.GUARDRAILS
    assert g.max_retries <= g.max_attempts_hard_cap
    assert g.contact_window_start_hour < g.contact_window_end_hour
    assert g.max_messages_per_customer >= 1


def test_vocab_is_consistent():
    assert "mandate_cancelled" in config.ROOT_CAUSES
    assert "send_mandate_link" in config.INTERVENTIONS
    assert "recovered" in config.STOP_REASONS
