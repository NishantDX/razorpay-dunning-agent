"""Tests for dunning/llm.py - offline heuristic, parsing, and the response cache.

The real Gemini call is never made here; ``_call_gemini`` is monkeypatched.
"""
import json

import pytest

from dunning import config, llm


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Keep every test off the real data/llm_cache.json."""
    monkeypatch.setattr(llm, "CACHE_FILE", tmp_path / "llm_cache.json")
    llm.reset_cache()
    yield
    llm.reset_cache()


# --- provider selection ---------------------------------------------------- #

def test_active_provider_falls_back_to_fake_without_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert llm.active_provider() == "fake"


def test_active_provider_is_gemini_with_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    assert llm.active_provider() == "gemini"


# --- fake classifier ----------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("txn declnd - insuff bal", "insufficient_funds"),
    ("customer told us the paycheck is late this month", "insufficient_funds"),
    ("the card on file is too old now", "expired_card"),
    ("issuer bank was flaky just now, lots of these", "bank_timeout"),
    ("PG error: upstream timed out (504)", "bank_timeout"),
    ("they turned off auto-pay for us", "mandate_cancelled"),
    ("customer walked away before paying", "abandoned"),
])
def test_fake_classify_hits(monkeypatch, text, expected):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    res = llm.classify_failure(text)
    assert res.model == "fake" and res.cached is False
    assert res.label == expected and 0.0 < res.confidence <= 1.0


def test_fake_classify_unknown(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    res = llm.classify_failure("the weather was nice and nothing else happened")
    assert res.label == "unknown" and res.confidence == 0.0


def test_empty_text_is_unknown():
    res = llm.classify_failure("   ")
    assert res.label == "unknown" and res.model == "none"


# --- JSON parsing ------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ('{"root_cause": "bank_timeout", "confidence": 0.9}', ("bank_timeout", 0.9)),
    ('```json\n{"root_cause":"expired_card","confidence":0.7}\n```', ("expired_card", 0.7)),
    ('the answer is insufficient_funds', ("insufficient_funds", 0.5)),
    ('total gibberish here', ("unknown", 0.0)),
    ('{"root_cause": "not_a_real_label", "confidence": 1}', ("unknown", 0.0)),
])
def test_parse_classify(raw, expected):
    assert llm._parse_classify(raw, list(config.ROOT_CAUSES)) == expected


# --- Gemini path + cache --------------------------------------------------- #

def _use_gemini(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")


def test_gemini_result_is_cached(monkeypatch, tmp_path):
    _use_gemini(monkeypatch)
    calls = []

    def fake_gemini(prompt, **kw):
        calls.append(prompt)
        return '{"root_cause": "mandate_cancelled", "confidence": 0.88}'

    monkeypatch.setattr(llm, "_call_gemini", fake_gemini)

    first = llm.classify_failure("some ambiguous failure text")
    assert first.label == "mandate_cancelled"
    assert first.cached is False and first.model == config.GEMINI_MODEL

    second = llm.classify_failure("some ambiguous failure text")
    assert second.label == "mandate_cancelled" and second.cached is True

    assert len(calls) == 1  # second answer came from cache
    saved = json.loads((tmp_path / "llm_cache.json").read_text())
    assert len(saved) == 1
    (entry,) = saved.values()
    assert entry["result"]["label"] == "mandate_cancelled"


def test_gemini_failure_never_raises(monkeypatch):
    _use_gemini(monkeypatch)

    def boom(prompt, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm, "_call_gemini", boom)
    res = llm.classify_failure("weird text the rules missed")
    assert res.label == "unknown" and res.confidence == 0.0
    assert res.cached is False


def test_choices_are_respected(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    res = llm.classify_failure("txn declnd - insuff bal", choices=["bank_timeout", "unknown"])
    assert res.label == "unknown"  # insufficient_funds not offered


# --- write_message (provisional) ----------------------------------------- #

def test_write_message_fake(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    msg = llm.write_message(customer_name="Asha", amount_rupees=499,
                            language="hinglish", ask="pay_link")
    assert msg.model == "fake"
    assert "Asha" in msg.text and "499" in msg.text
