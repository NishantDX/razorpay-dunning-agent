"""dunning/llm.py - the one place the project talks to an LLM.

Two narrow jobs, by design:

* ``classify_failure()`` - a messy free-text failure reason -> one canonical root
  cause. The diagnoser (step 4) calls this only for text its rules table can't
  place; the clean ~85% never reach here.
* ``write_message()`` - draft the customer nudge (step 8 owns this; a provisional
  implementation lives here so the module is complete).

Everything else in the agent stays deterministic code.

Provider: ``LLM_PROVIDER`` (default ``gemini``; key from aistudio.google.com,
free). If the provider is ``gemini`` but no ``GEMINI_API_KEY`` is set we fall
back to a small offline heuristic (``fake``) so the pipeline and the test-suite
run with zero setup. Set the key to use the real classifier.

Cache: every *real* LLM answer is written to ``data/llm_cache.json`` keyed by a
hash of the exact input (function + model + payload). Re-running the batch then
makes no API calls and is byte-for-byte reproducible - which also keeps the audit
trail deterministic. The offline ``fake`` path is already deterministic, so it is
not cached.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from tenacity import retry, stop_after_attempt, wait_exponential

from dunning import config

CACHE_FILE = config.DATA_DIR / "llm_cache.json"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ClassifyResult:
    label: str          # one of the allowed choices, or "unknown"
    confidence: float    # 0.0 .. 1.0
    model: str           # "gemini-2.0-flash" | "fake" | "none"
    cached: bool


@dataclass(frozen=True)
class MessageResult:
    text: str
    model: str
    cached: bool


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #

def active_provider() -> str:
    """Read at call time so tests can flip config."""
    provider = (config.LLM_PROVIDER or "gemini").strip().lower()
    if provider == "gemini" and not config.GEMINI_API_KEY:
        return "fake"
    return provider


def _model_label(provider: str) -> str:
    return config.GEMINI_MODEL if provider == "gemini" else provider


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

_CACHE = None


def reset_cache() -> None:
    """Drop the in-memory cache (tests call this after pointing CACHE_FILE at a
    temp path)."""
    global _CACHE
    _CACHE = None


def _cache() -> dict:
    global _CACHE
    if _CACHE is None:
        try:
            _CACHE = json.loads(Path(CACHE_FILE).read_text("utf-8")) if Path(CACHE_FILE).exists() else {}
        except (json.JSONDecodeError, OSError):
            _CACHE = {}
    return _CACHE


def _cache_put(key: str, entry: dict) -> None:
    cache = _cache()
    cache[key] = entry
    try:
        Path(CACHE_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(CACHE_FILE).write_text(
            json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # a broken cache must never break a run


def _key(fn: str, model: str, payload: dict) -> str:
    blob = json.dumps(
        {"fn": fn, "model": model, "payload": payload}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Gemini call (isolated so tests can monkeypatch it)
# --------------------------------------------------------------------------- #

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8),
       reraise=True)
def _call_gemini(prompt: str, *, temperature: float = 0.0) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.GEMINI_API_KEY)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=temperature),
    )
    return (response.text or "").strip()


# --------------------------------------------------------------------------- #
# classify_failure
# --------------------------------------------------------------------------- #

_CLASSIFY_PROMPT = """You classify a failed payment by its underlying root cause.

Allowed root causes: {choices}
Use "unknown" only when none of them clearly fits.

Reply with JSON and nothing else:
{{"root_cause": "<one allowed value>", "confidence": <number between 0 and 1>}}

Failure message:
\"\"\"{text}\"\"\"
"""

# Offline heuristic. Deliberately broader than the diagnoser's (literal-only)
# rules table - it leans on semantic cues - but it is a fixed list, so a real
# Gemini key still classifies things this misses. Order matters: the more
# specific causes are checked first.
_FAKE_PATTERNS = [
    ("mandate_cancelled", re.compile(
        r"mandate|auto[- ]?pay|autopay|auto[- ]?debit|e[- ]?nach|token rejected"
        r"|recurring permission|standing instruction|turned off auto|revok", re.I)),
    ("expired_card", re.compile(
        r"expired|expiry|exp\.? ?(?:date|\d)|code 54|too old|fresh card|new card"
        r"|new instrument|no longer valid|instrument no longer", re.I)),
    ("abandoned", re.compile(
        r"abandon|never (?:reached|finished|completed)|did ?n[o']t complete"
        r"|dropped (?:at|off)|walked away|left the page|no attempt|0 payment attempts"
        r"|nothing was charged", re.I)),
    ("bank_timeout", re.compile(
        r"time ?d? ?out|no response|did not respond|50[24]\b|upstream|npci|flaky"
        r"|hiccup|socket closed|bank.{0,20}down", re.I)),
    ("insufficient_funds", re.compile(
        r"insuffic|insuff|low (?:funds|bal)|do not honou?r|\bnsf\b|not funded"
        r"|funded enough|paycheck|salary|balance too low|not enough (?:funds|balance)", re.I)),
]


def _fake_classify(text: str, choices: Sequence[str]) -> tuple:
    for label, pattern in _FAKE_PATTERNS:
        if label in choices and pattern.search(text):
            return label, 0.75
    return "unknown", 0.0


def _parse_classify(raw: str, choices: Sequence[str]) -> tuple:
    match = re.search(r"\{.*\}", raw, re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            label = str(obj.get("root_cause", "")).strip().lower()
            conf = float(obj.get("confidence", 0.0))
            if label in choices:
                return label, max(0.0, min(1.0, conf))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    low = raw.lower()
    for label in choices:
        if label in low:
            return label, 0.5
    return "unknown", 0.0


def classify_failure(text: str, choices: Sequence[str] = config.ROOT_CAUSES) -> ClassifyResult:
    text = (text or "").strip()
    choices = list(choices)
    if not text:
        return ClassifyResult("unknown", 0.0, "none", False)

    provider = active_provider()
    if provider == "fake":
        label, conf = _fake_classify(text, choices)
        return ClassifyResult(label, conf, "fake", False)

    model = _model_label(provider)
    key = _key("classify_failure", model, {"text": text, "choices": choices})
    hit = _cache().get(key)
    if hit:
        res = hit["result"]
        return ClassifyResult(res["label"], res["confidence"], model, True)

    prompt = _CLASSIFY_PROMPT.format(choices=", ".join(choices), text=text)
    try:
        raw = _call_gemini(prompt)
    except Exception:
        return ClassifyResult("unknown", 0.0, model, False)  # never break the batch

    label, conf = _parse_classify(raw, choices)
    _cache_put(key, {
        "fn": "classify_failure", "model": model,
        "result": {"label": label, "confidence": conf},
        "input_preview": text[:120], "ts": _now(),
    })
    return ClassifyResult(label, conf, model, False)


# --------------------------------------------------------------------------- #
# write_message  (provisional - step 8 owns the real contract)
# --------------------------------------------------------------------------- #

_MESSAGE_PROMPT = """Write a short, polite payment-recovery nudge to a customer.

Constraints:
- 2 sentences, under 40 words, no emojis.
- Language: {language} (if "hinglish", mix natural Hindi + English in Latin script).
- Mention the amount as Rs {amount_rupees}.
- Ask them to {ask}. Do not threaten or blame.

Return only the message text.
"""

_FAKE_MESSAGE = {
    "en": "Hi {name}, your payment of Rs {amount_rupees} didn't go through. "
          "Please {ask} so we can complete it.",
    "hi": "Namaste {name}, aapka Rs {amount_rupees} ka payment nahi ho paaya. "
          "Kripya {ask}.",
    "hinglish": "Hi {name}, aapka Rs {amount_rupees} ka payment fail ho gaya. "
                "Please {ask} taaki hum ise complete kar sakein.",
}

_ASK_TEXT = {
    "retry": "let us try the charge again",
    "update_method": "add a different card or payment method",
    "pay_link": "use the payment link we sent",
    "reauthorise_mandate": "re-approve the auto-pay mandate",
}


def write_message(*, customer_name: str, amount_rupees: int, language: str = "en",
                  ask: str = "retry") -> MessageResult:
    language = language if language in _FAKE_MESSAGE else "en"
    ask_text = _ASK_TEXT.get(ask, ask)

    provider = active_provider()
    if provider == "fake":
        text = _FAKE_MESSAGE[language].format(
            name=customer_name, amount_rupees=amount_rupees, ask=ask_text
        )
        return MessageResult(text, "fake", False)

    model = _model_label(provider)
    payload = {"name": customer_name, "amount_rupees": amount_rupees,
               "language": language, "ask": ask_text}
    key = _key("write_message", model, payload)
    hit = _cache().get(key)
    if hit:
        return MessageResult(hit["result"]["text"], model, True)

    prompt = _MESSAGE_PROMPT.format(language=language, amount_rupees=amount_rupees,
                                    ask=ask_text)
    try:
        text = _call_gemini(prompt, temperature=0.4)
    except Exception:
        text = _FAKE_MESSAGE[language].format(
            name=customer_name, amount_rupees=amount_rupees, ask=ask_text
        )
        return MessageResult(text, model, False)

    _cache_put(key, {"fn": "write_message", "model": model,
                     "result": {"text": text}, "ts": _now()})
    return MessageResult(text, model, False)
