"""Step 8 - the customer message writer.

The LLM's second (and last) job: draft the nudge a customer sees when the agent
sends a reminder, a payment link, or a mandate re-authorisation link.

Per channel, because people read them differently:

* **sms**      - <=160 chars, plain, transactional, no emoji
* **whatsapp** - 2-3 short lines, conversational, at most one emoji

Per language: ``en`` (English), ``hi`` (Hindi, Devanagari), ``hinglish``
(Hindi + English in Latin script).

Every generated message is checked by a deterministic validator
(``_validate``): it must state the amount, must carry the link when one is
supplied, must not threaten or invent consequences, and must not leak a raw
email or phone number. If the LLM output fails, or there is no API key, a fixed
template is used instead - so the pipeline always produces a safe message.

We do NOT tailor tone / A-B variants: there are no real recipients to measure a
lift against, so a single polite, plain register is used everywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from dunning import llm, redact

CHANNELS = ("sms", "whatsapp")
_ASKS = {
    "retry": {
        "en": "we'll try the payment again",
        "hi": "hum payment dobara try karenge",
        "hinglish": "hum payment dobara try karenge",
    },
    "pay_link": {
        "en": "please use this link to complete it",
        "hi": "ise poora karne ke liye is link ka upyog karein",
        "hinglish": "ise complete karne ke liye ye link use karein",
    },
    "reauthorise_mandate": {
        "en": "please re-approve the auto-pay mandate using this link",
        "hi": "is link se auto-pay mandate dobara approve karein",
        "hinglish": "is link se auto-pay mandate ko dobara approve karein",
    },
}

_THREAT_WORDS = re.compile(
    r"\b(legal|lawsuit|court|police|penalty|penalties|fine|blacklist|"
    r"recovery agent|consequences|seiz(e|ed|ure)|arrest|defaulter|"
    r"block your account|report you)\b", re.I)
_EMAILISH = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONEISH = re.compile(r"(?<!\d)(?:\+?91[\-\s]?)?\d{10}(?!\d)")

_MAX_LEN = {"sms": 200, "whatsapp": 500}


@dataclass(frozen=True)
class Message:
    channel: str
    language: str
    body: str
    model: str            # gemini model id | "template"
    cached: bool = False
    from_template: bool = False
    issues: tuple = field(default_factory=tuple)   # validator findings (empty = clean)


# --------------------------------------------------------------------------- #
# Templates - always safe, used when the LLM is off or its output fails review
# --------------------------------------------------------------------------- #

_TEMPLATES = {
    "sms": {
        "en": "Hi {name}, your payment of Rs {amt} did not go through. {ask}{link}. Reply STOP to opt out.",
        "hi": "Namaste {name}, aapka Rs {amt} ka payment nahi hua. {ask}{link}. Rokne ke liye STOP bhejein.",
        "hinglish": "Hi {name}, aapka Rs {amt} ka payment fail ho gaya. {ask}{link}. Opt out ke liye STOP reply karein.",
    },
    "whatsapp": {
        "en": "Hi {name} \U0001f44b\nYour payment of Rs {amt} didn't go through.\n{ask}{link}\nReply here if you need help.",
        "hi": "Namaste {name} \U0001f44b\nAapka Rs {amt} ka payment nahi ho paaya.\n{ask}{link}\nMadad chahiye to yahin reply karein.",
        "hinglish": "Hi {name} \U0001f44b\nAapka Rs {amt} ka payment fail ho gaya.\n{ask}{link}\nKoi help chahiye to yahin reply karein.",
    },
}


def _fill_template(channel: str, language: str, name: str, amt: int, ask: str,
                   link_url: str) -> str:
    link = f": {link_url}" if link_url else ""
    return _TEMPLATES[channel][language].format(name=name, amt=f"{amt:,}", ask=ask, link=link)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_PROMPT = """Write ONE {channel_desc} to a customer whose payment just failed.

Rules:
- Language: {lang_desc}
- State the amount exactly as "Rs {amt}".
- The single ask: {ask}.
{link_rule}
- Polite and plain. Never threaten, never mention legal action, penalties,
  agents, or account blocks. Do not invent consequences.
- No email addresses or phone numbers in the text.
{len_rule}

Return only the message text, nothing else.
"""

_CHANNEL_DESC = {
    "sms": "SMS (single message, at most 160 characters, plain text, no emoji)",
    "whatsapp": "WhatsApp message (2-3 short lines, friendly, at most one emoji)",
}
_LANG_DESC = {
    "en": "English",
    "hi": "Hindi in Devanagari script",
    "hinglish": "Hinglish - Hindi and English mixed, written in Latin script",
}


def _build_prompt(channel: str, language: str, amt: int, ask: str, link_url: str) -> str:
    return _PROMPT.format(
        channel_desc=_CHANNEL_DESC[channel],
        lang_desc=_LANG_DESC[language],
        amt=f"{amt:,}",
        ask=ask,
        link_rule=(f'- Include this link exactly once: {link_url}' if link_url
                   else "- Do not include any link."),
        len_rule=("- Keep it under 160 characters." if channel == "sms"
                  else "- Keep it under 90 words."),
    )


# --------------------------------------------------------------------------- #
# Validator
# --------------------------------------------------------------------------- #

def _validate(body: str, *, channel: str, amt: int, link_url: str) -> list:
    issues = []
    if not body or "{" in body or "}" in body:
        issues.append("empty or unfilled")
        return issues
    if str(amt) not in body and f"{amt:,}" not in body:
        issues.append("amount missing")
    if link_url and link_url not in body:
        issues.append("link missing")
    if not link_url and _EMAILISH.search(body):
        issues.append("contains an email address")
    if _PHONEISH.search(body.replace(link_url, "") if link_url else body):
        issues.append("contains a phone number")
    if _THREAT_WORDS.search(body):
        issues.append("threatening / coercive language")
    if len(body) > _MAX_LEN[channel]:
        issues.append(f"too long ({len(body)} > {_MAX_LEN[channel]})")
    return issues


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def write(channel: str, *, customer_name: str, amount_rupees: int, language: str = "en",
          ask: str = "retry", link_url: str = "") -> Message:
    if channel not in CHANNELS:
        channel = "sms"
    if language not in _LANG_DESC:
        language = "en"
    name = redact.first_name(customer_name)
    ask_phrase = _ASKS.get(ask, _ASKS["retry"])[language]

    comp = llm.complete(
        _build_prompt(channel, language, amount_rupees, ask_phrase, link_url),
        temperature=0.3,
        cache_bucket="message",
        cache_payload={"channel": channel, "language": language, "ask": ask,
                       "amount_rupees": amount_rupees, "has_link": bool(link_url)},
    )
    body = (comp.text or "").strip()
    issues = _validate(body, channel=channel, amt=amount_rupees, link_url=link_url) if body else ["no LLM output"]

    if issues:
        body = _fill_template(channel, language, name, amount_rupees, ask_phrase, link_url)
        return Message(channel, language, body, "template", comp.cached, True, tuple(issues))
    return Message(channel, language, body, comp.model, comp.cached, False, ())


def compose(*, customer_name: str, amount_rupees: int, language: str = "en",
            ask: str = "retry", link_url: str = "") -> dict:
    """A message for every channel, keyed by channel name."""
    return {
        ch: write(ch, customer_name=customer_name, amount_rupees=amount_rupees,
                  language=language, ask=ask, link_url=link_url)
        for ch in CHANNELS
    }
