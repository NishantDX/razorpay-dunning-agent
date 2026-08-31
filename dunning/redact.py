"""Data minimisation helpers.

The audit log and the report must never carry raw customer PII or anything that
looks like a secret. Everything written out for humans / storage goes through
here first.

* `mask_phone`  / `mask_email` - keep just enough to recognise a record
* `first_name`  - drop everything after the first token
* `sanitize`    - strip token-like secrets from free text (e.g. an exception
                  string that happened to contain a key or an auth URL)
* `redact_customer` - a customer dict -> a safe-to-log dict
"""
from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"rzp_(?:live|test)_[A-Za-z0-9]+"),           # Razorpay key ids
    re.compile(r"(?i)(?:api[_-]?)?(?:key|secret|token|password|authorization|bearer)\s*[=:]\s*\S+"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                   # Google API keys
    re.compile(r"https?://[^\s]*[?&](?:key|secret|token|sig)=[^\s&]+"),
]


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "***"
    return f"{digits[:2]}***{digits[-2:]}" if len(digits) <= 8 else f"{digits[:4]}***{digits[-2:]}"


def mask_email(email: str) -> str:
    email = email or ""
    if "@" not in email:
        return "***"
    local, _, domain = email.partition("@")
    dom_name, _, tld = domain.rpartition(".")
    keep = local[:1] if local else ""
    return f"{keep}***@{(dom_name[:1] + '***') if dom_name else '***'}.{tld or '***'}"


def first_name(name: str) -> str:
    return (name or "").strip().split(" ")[0] or "there"


def sanitize(text: str) -> str:
    out = text or ""
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[redacted]", out)
    return out


def redact_customer(customer: dict) -> dict:
    return {
        "customer_id": customer.get("customer_id", ""),
        "name": first_name(customer.get("name", "")),
        "email": mask_email(customer.get("email", "")),
        "contact": mask_phone(customer.get("contact", "")),
        "language": customer.get("language", ""),
        "reachable": customer.get("reachable", None),
        "prior_payments": customer.get("prior_payments", None),
    }
