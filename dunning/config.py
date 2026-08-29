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
POLICY_FILE = ROOT / "config" / "policy.yaml"


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
    "written_off",           # deliberately abandoned (e.g. value below effort)
)

# --- Canonical root causes -------------------------------------------------- #
# The diagnoser maps every raw failure to exactly one of these. Grouped by how
# recovery must be approached; the grouping drives the policy engine (step 5).
ROOT_CAUSES = (
    # funds / limits - the instrument is fine, the money or headroom is not
    "insufficient_funds",
    "card_limit_exceeded",
    # transient infrastructure - likely to clear on its own
    "bank_timeout",
    "issuer_unavailable",
    "technical_decline",
    # authentication - the charge needs the customer to re-authenticate
    "three_ds_failed",
    # instrument unusable - a different payment method is required
    "expired_card",
    "invalid_payment_details",
    "international_blocked",
    # issuer refused without a specific reason - do not hammer it
    "do_not_honour",
    # security - retrying is harmful; never do it
    "card_declined_risk",
    "stolen_or_lost_card",
    # subscription mandate is dead
    "mandate_cancelled",
    # no payment was ever attempted
    "abandoned",
    # diagnoser not confident enough - a human decides
    "needs_review",
)

# Causes where re-attempting the SAME charge is a legitimate move.
RETRY_SAFE_CAUSES = frozenset({
    "insufficient_funds", "card_limit_exceeded", "bank_timeout",
    "issuer_unavailable", "technical_decline", "do_not_honour",
})
# Causes where re-attempting is pointless or actively harmful.
NEVER_RETRY_CAUSES = frozenset({
    "card_declined_risk", "stolen_or_lost_card", "mandate_cancelled",
    "expired_card", "invalid_payment_details", "international_blocked",
})

# --- Interventions the policy engine may choose --------------------------- #
INTERVENTIONS = (
    "retry_now",           # immediate re-attempt of the same charge
    "retry_later",         # scheduled re-attempt of the same charge
    "send_reminder",       # message only, no payment action
    "send_payment_link",   # Razorpay Payment Link + message (any method)
    "send_mandate_link",   # re-authorise the subscription mandate + message
    "switch_method",       # re-attempt on a different instrument already on file
    "handoff_human",       # escalate with a full case summary
    "do_nothing",          # deliberately stop (e.g. value below recovery effort)
)
# Interventions that put a message in front of the customer (message cap applies).
MESSAGE_INTERVENTIONS = frozenset({
    "send_reminder", "send_payment_link", "send_mandate_link",
})
# Interventions that re-attempt the charge (retry cap applies).
RETRY_INTERVENTIONS = frozenset({"retry_now", "retry_later"})
