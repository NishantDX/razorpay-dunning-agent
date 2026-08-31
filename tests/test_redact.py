"""Tests for the PII / secret redaction helpers."""
from dunning import redact


def test_mask_phone_keeps_only_ends():
    m = redact.mask_phone("+919812345678")
    assert m.startswith("9198") and m.endswith("78") and "***" in m
    assert "812345" not in m


def test_mask_email():
    assert redact.mask_email("krista.klein@example.com") == "k***@e***.com"
    assert redact.mask_email("bad") == "***"


def test_first_name_only():
    assert redact.first_name("Dawn Marie Horne") == "Dawn"
    assert redact.first_name("") == "there"


def test_sanitize_strips_secret_like_tokens():
    s = redact.sanitize("boom rzp_test_ABCD1234efgh and api_key=SUPERSECRET done")
    assert "rzp_test_ABCD1234efgh" not in s
    assert "SUPERSECRET" not in s
    assert "[redacted]" in s


def test_sanitize_strips_google_key_and_query_secret():
    s = redact.sanitize("GET https://x.test/pay?token=abc123 key=AIzaSyA1234567890abcdefghijklmnopqrstuvw")
    assert "AIzaSyA1234567890abcdefghijklmnopqrstuvw" not in s
    assert "token=abc123" not in s


def test_redact_customer_drops_raw_pii():
    c = {"customer_id": "cust_1", "name": "Dawn Horne", "email": "dawn@acme.co",
         "contact": "+919812345678", "language": "en", "reachable": True,
         "prior_payments": 3, "prior_success_rate": 0.9}
    r = redact.redact_customer(c)
    assert r["name"] == "Dawn"
    assert "@" in r["email"] and "dawn@acme.co" != r["email"]
    assert r["contact"] != c["contact"]
    assert "prior_success_rate" not in r
