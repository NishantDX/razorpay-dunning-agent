"""Central configuration: environment, guardrail limits, stopping rules, and
the shared vocabulary (root causes, interventions).

Everything tunable lives here so the guardrails are auditable in one file.
No business logic - just constants and simple loaders.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Paths ---
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
for _d in (DATA_DIR, REPORTS_DIR, LOGS_DIR):
    _d.mkdir(exist_ok=True)

CASES_FILE = DATA_DIR / "cases.jsonl"
EVENTS_FILE = DATA_DIR / "events.jsonl"
AUDIT_LOG = LOGS_DIR / "audit.jsonl"


# --- Environment ---
def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


LLM_PROVIDER = _get("LLM_PROVIDER", "gemini")
GEMINI_API_KEY = _get("GEMINI_API_KEY")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.0-flash")

RAZORPAY_KEY_ID = _get("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = _get("RAZORPAY_KEY_SECRET")
RAZORPAY_DRY_RUN = _get("RAZORPAY_DRY_RUN", "0") == "1"

RANDOM_SEED = int(_get("RANDOM_SEED", "42") or "42")
BATCH_SIZE = int(_get("BATCH_SIZE", "300") or "300")


# --- Guardrails: hard limits the agent may never exceed ---
@dataclass(frozen=True)
class Guardrails:
    max_retries: int = 3                  # per case, total payment re-attempts
    min_hours_between_attempts: int = 24  # spacing between attempts
    contact_window_start_hour: int = 9    # local time, inclusive
    contact_window_end_hour: int = 20     # local time, exclusive
    max_messages_per_customer: int = 2    # across the whole sequence
    max_attempts_hard_cap: int = 5        # absolute backstop across all actions


GUARDRAILS = Guardrails()

# --- Stopping rules: the sequence halts immediately if any is true ---
STOP_ON_CUSTOMER_REPLIES = ("paid", "stop", "dispute", "unsubscribe")
STOP_REASONS = (
    "recovered",             # money arrived - success
    "customer_opted_out",    # reply matched STOP_ON_CUSTOMER_REPLIES
    "mandate_dead",          # subscription mandate cancelled / revoked
    "max_retries_reached",
    "escalated_to_human",
)

# --- Canonical root causes: the diagnoser maps every raw reason to one of these ---
ROOT_CAUSES = (
    "insufficient_funds",
    "expired_card",
    "bank_timeout",
    "mandate_cancelled",
    "abandoned",
    "unknown",
)

# --- Interventions the policy engine may choose ---
INTERVENTIONS = (
    "retry_later",
    "retry_now",
    "switch_method",
    "send_payment_link",
    "send_mandate_link",
    "handoff_human",
    "do_nothing",
)
