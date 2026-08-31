"""Tests for the step 8 message writer."""
import pytest

from dunning import config, llm, messaging


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No API key -> llm.complete returns "" -> every message is a template.
    Also keep the LLM cache off the real data dir."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    monkeypatch.setattr(llm, "CACHE_FILE", tmp_path / "llm_cache.json")
    llm.reset_cache()
    yield
    llm.reset_cache()


@pytest.mark.parametrize("channel", messaging.CHANNELS)
@pytest.mark.parametrize("language", ["en", "hi", "hinglish"])
def test_template_message_is_safe_and_complete(channel, language):
    m = messaging.write(channel, customer_name="Asha Verma", amount_rupees=1499,
                        language=language, ask="pay_link", link_url="https://rzp.io/i/abcd1234")
    assert m.from_template and m.model == "template"
    assert "Asha" in m.body and "1,499" in m.body
    assert "https://rzp.io/i/abcd1234" in m.body
    assert "Verma" not in m.body                  # surname dropped
    assert len(m.body) <= messaging._MAX_LEN[channel]
    assert not messaging._THREAT_WORDS.search(m.body)


def test_compose_covers_every_channel():
    msgs = messaging.compose(customer_name="Ravi", amount_rupees=200, language="en",
                             ask="retry")
    assert set(msgs) == set(messaging.CHANNELS)
    assert all(isinstance(v, messaging.Message) for v in msgs.values())


def test_sms_has_no_emoji_from_template():
    m = messaging.write("sms", customer_name="Ravi", amount_rupees=200, language="en", ask="retry")
    assert all(ord(c) < 0x1F000 for c in m.body)


# --- validator ------------------------------------------------------- #

def test_validator_flags_threats():
    issues = messaging._validate("Pay Rs 500 now or we will take legal action",
                                 channel="sms", amt=500, link_url="")
    assert any("threat" in i for i in issues)


def test_validator_flags_missing_amount_and_link():
    issues = messaging._validate("Please pay using the link", channel="sms", amt=500,
                                 link_url="https://x.test/y")
    assert "amount missing" in issues and "link missing" in issues


def test_validator_flags_leaked_pii():
    issues = messaging._validate("Rs 500 - contact us at ops@acme.co or 9876543210",
                                 channel="whatsapp", amt=500, link_url="")
    assert any("email" in i for i in issues) and any("phone" in i for i in issues)


def test_validator_passes_a_clean_message():
    body = "Hi Asha, your payment of Rs 500 did not go through. Please use this link: https://x.test/y"
    assert messaging._validate(body, channel="sms", amt=500, link_url="https://x.test/y") == []


# --- LLM path with validation + fallback ---------------------------- #

def test_bad_llm_output_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(llm, "_call_gemini",
                        lambda *a, **k: "PAY NOW or face legal action and penalty")
    m = messaging.write("sms", customer_name="Asha", amount_rupees=500, language="en", ask="retry")
    assert m.from_template is True
    assert m.issues  # records why the LLM output was rejected


def test_good_llm_output_is_used(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "key")
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    good = "Hi Asha, your payment of Rs 500 did not go through. We will try the payment again."
    monkeypatch.setattr(llm, "_call_gemini", lambda *a, **k: good)
    m = messaging.write("sms", customer_name="Asha", amount_rupees=500, language="en", ask="retry")
    assert m.from_template is False and m.body == good
