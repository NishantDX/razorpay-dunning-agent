"""Step 7 - the guardrail layer.

Every hard limit the agent must respect lives here, in one place, and is applied
the same way for every case:

* ``GuardrailLedger`` - per case. The executor asks ``evaluate(action, when)``
  *before* each step and gets a `Decision`: allow it, defer it to a later time
  (retry spacing, contact window), skip it (retry / message cap), or halt the
  sequence (hard action cap). Every decision is kept, so the audit log can show
  *why* each action ran when it did - or didn't run.
* ``SpendGovernor`` - per run. Caps the total money a batch may attempt and
  trips a circuit breaker after too many gateway errors.
* ``count_violations`` / ``assert_no_violations`` - an independent recomputation
  from the finished attempt logs. Because the ledger makes breaches impossible,
  this is expected to be 0 every time; the batch asserts it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from dunning import config

G = config.GUARDRAILS
M = config.MONEY


def in_contact_window(dt: datetime) -> bool:
    return G.contact_window_start_hour <= dt.hour < G.contact_window_end_hour


def to_contact_window(dt: datetime) -> datetime:
    if dt.hour < G.contact_window_start_hour:
        return dt.replace(hour=G.contact_window_start_hour, minute=0, second=0, microsecond=0)
    return (dt + timedelta(days=1)).replace(
        hour=G.contact_window_start_hour, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Decision:
    action: str
    kind: str        # "allow" | "defer" | "skip" | "halt"
    at: datetime
    rule: str        # "clear" | "retry_spacing" | "contact_window" | "retry_cap"
                     # | "message_cap" | "action_hard_cap"
    note: str = ""


class GuardrailLedger:
    """One per case. Owns retry spacing, the contact window, and all the caps."""

    def __init__(self):
        self._retry_times: list = []
        self._messages = 0
        self._actions = 0
        self.decisions: list = []

    # -- counters (read-only-ish) --
    @property
    def retries_used(self) -> int:
        return len(self._retry_times)

    @property
    def messages_sent(self) -> int:
        return self._messages

    @property
    def actions_taken(self) -> int:
        return self._actions

    def evaluate(self, action: str, desired_at: datetime) -> Decision:
        if self._actions >= G.max_attempts_hard_cap:
            d = Decision(action, "halt", desired_at, "action_hard_cap",
                         f"{self._actions} actions already taken (cap {G.max_attempts_hard_cap})")
        elif action in config.RETRY_INTERVENTIONS and self.retries_used >= G.max_retries:
            d = Decision(action, "skip", desired_at, "retry_cap",
                         f"{self.retries_used} retries already used (cap {G.max_retries})")
        elif action in config.MESSAGE_INTERVENTIONS and self._messages >= G.max_messages_per_customer:
            d = Decision(action, "skip", desired_at, "message_cap",
                         f"{self._messages} messages already sent (cap {G.max_messages_per_customer})")
        else:
            at, rule = desired_at, "clear"
            if action in config.RETRY_INTERVENTIONS and self._retry_times:
                floor = self._retry_times[-1] + timedelta(hours=G.min_hours_between_attempts)
                if at < floor:
                    at, rule = floor, "retry_spacing"
            if action in config.MESSAGE_INTERVENTIONS and not in_contact_window(at):
                at, rule = to_contact_window(at), "contact_window"
            d = Decision(action, "allow" if rule == "clear" else "defer", at, rule)
        self.decisions.append(d)
        return d

    def record(self, action: str, at: datetime) -> None:
        """Call once the executor has actually performed the action."""
        self._actions += 1
        if action in config.RETRY_INTERVENTIONS:
            self._retry_times.append(at)
        if action in config.MESSAGE_INTERVENTIONS:
            self._messages += 1


@dataclass
class SpendGovernor:
    """One per run. The batch stops launching new cases once either limit bites."""
    attempted_paise: int = 0
    gateway_errors: int = 0
    blocked_cases: int = 0

    def may_attempt(self, paise: int) -> bool:
        return self.attempted_paise + int(paise) <= M.max_total_attempted_paise

    def note_attempt(self, paise: int) -> None:
        self.attempted_paise += int(paise)

    def note_gateway_error(self) -> None:
        self.gateway_errors += 1

    def tripped(self) -> str:
        if self.gateway_errors >= M.max_gateway_errors:
            return f"circuit breaker: {self.gateway_errors} gateway errors"
        if self.attempted_paise > M.max_total_attempted_paise:
            return f"run spend ceiling reached: Rs {self.attempted_paise // 100:,}"
        return ""


# --------------------------------------------------------------------------- #
# Independent post-hoc check (should always be zero)
# --------------------------------------------------------------------------- #

# outcomes that mean the action was actually carried out (vs skipped / aborted)
_RETRY_DONE = {"recovered", "failed"}
_MESSAGE_DONE = {"sent", "recovered", "customer_opted_out"}


def _violations_for(attempts) -> list:
    out = []
    retry_times = [a.at for a in attempts
                   if a.action in config.RETRY_INTERVENTIONS and a.outcome in _RETRY_DONE]
    messages = [a for a in attempts
                if a.action in config.MESSAGE_INTERVENTIONS and a.outcome in _MESSAGE_DONE]
    if len(retry_times) > G.max_retries:
        out.append(f"retries={len(retry_times)} > {G.max_retries}")
    if len(messages) > G.max_messages_per_customer:
        out.append(f"messages={len(messages)} > {G.max_messages_per_customer}")
    for earlier, later in zip(retry_times, retry_times[1:]):
        if later - earlier < timedelta(hours=G.min_hours_between_attempts):
            out.append(f"retry gap {later - earlier} < {G.min_hours_between_attempts}h")
    for a in messages:
        if not in_contact_window(a.at):
            out.append(f"message at {a.at.isoformat()} outside contact window")
    performed = [a for a in attempts if a.outcome in _RETRY_DONE | _MESSAGE_DONE
                 | {"escalated", "noop"}]
    if len(performed) > G.max_attempts_hard_cap + 1:
        out.append("action hard cap exceeded")
    return out


def count_violations(results) -> int:
    return sum(len(_violations_for(r.attempts)) for r in results)


def assert_no_violations(results) -> None:
    offenders = {r.case_id: _violations_for(r.attempts) for r in results}
    offenders = {k: v for k, v in offenders.items() if v}
    if offenders:
        raise AssertionError(f"guardrail violations in {len(offenders)} case(s): "
                             + "; ".join(f"{k}: {v}" for k, v in list(offenders.items())[:5]))
