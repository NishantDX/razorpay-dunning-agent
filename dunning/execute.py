"""Step 6 - the executor.

Walks a `Plan` (step 5) one step at a time against **real Razorpay test-mode
APIs**, advancing a virtual clock between steps, and after each attempt decides
whether the money actually arrived.

What is real vs simulated (and why):

* **Real Razorpay test-mode calls.** `send_payment_link` / `send_mandate_link`
  really create a Razorpay Payment Link; a `retry_*` really creates a fresh
  Razorpay Order for the amount. Every create carries an idempotency key, and the
  gateway wrapper dedupes on it so a step can never double-charge.
* **Simulated: did the customer actually pay.** Test mode has no real card or
  person behind our synthetic cases, so the *outcome* of an attempt is rolled
  from the case's hidden `latent` parameters with a seeded RNG. This is the only
  simulated part, it is fully reproducible, and the agent never sees `latent`.

With `RAZORPAY_DRY_RUN=1`, or with no API keys set, the gateway falls back to a
local fake with the same shape so the batch runs with zero setup.

Re-planning: if a retry is aborted because the mandate died mid-sequence, and the
plan allows it, the executor re-plans **once** (to mandate repair) and continues.
That is the single cause-shift the simulator produces today; other shifts would
plug into `_cause_shift`.

CLI:  python -m dunning.execute   # diagnose -> plan -> execute every event
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from dunning import config, diagnose, feed, llm, policy, redact

G = config.GUARDRAILS
M = config.MONEY


def _amount_ok(paise: int) -> bool:
    return M.min_single_action_paise <= int(paise) <= M.max_single_action_paise


# --------------------------------------------------------------------------- #
# Virtual clock
# --------------------------------------------------------------------------- #

class Clock:
    """A simulated 'now' the executor fast-forwards between steps. Never moves
    backwards."""

    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance_to(self, when: datetime) -> None:
        if when > self._now:
            self._now = when


# --------------------------------------------------------------------------- #
# Razorpay gateway (real test-mode client, or a local fake)
# --------------------------------------------------------------------------- #

def _fake_id(prefix: str, key: str) -> str:
    """Deterministic id for the local fake, derived from the idempotency key so a
    repeated call yields the same id - just as a real idempotent create would."""
    return prefix + hashlib.sha256(key.encode()).hexdigest()[:14]


class RazorpayGateway:
    """Thin wrapper over the Razorpay client. Owns idempotency: a repeated key
    returns the first response instead of creating a second object."""

    def __init__(self, client, live: bool, store_path=None):
        self._client = client
        self.live = live
        self._store_path = Path(store_path) if store_path else None
        self._by_key: dict = {}
        self.calls: list = []      # (method, idempotency_key) - for audit / tests
        if self._store_path and self._store_path.exists():
            try:
                self._by_key = json.loads(self._store_path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                self._by_key = {}

    # -- internal --
    def _remember(self, key: str, resp: dict) -> dict:
        self._by_key[key] = resp
        if self._store_path:  # survive across runs so a re-run cannot double-charge
            try:
                self._store_path.parent.mkdir(parents=True, exist_ok=True)
                self._store_path.write_text(json.dumps(self._by_key), encoding="utf-8")
            except OSError:
                pass
        return resp

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4),
           reraise=True)
    def _live_order(self, data: dict) -> dict:
        return dict(self._client.order.create(data))

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=4),
           reraise=True)
    def _live_link(self, data: dict) -> dict:
        return dict(self._client.payment_link.create(data))

    # -- public --
    def create_order(self, amount_paise: int, notes: dict, idempotency_key: str) -> dict:
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        self.calls.append(("create_order", idempotency_key))
        if self.live:
            resp = self._live_order({"amount": amount_paise, "currency": "INR",
                                     "notes": notes})
        else:
            resp = {"id": _fake_id("order_", idempotency_key), "entity": "order",
                    "amount": amount_paise, "currency": "INR", "status": "created",
                    "notes": notes}
        return self._remember(idempotency_key, resp)

    def create_payment_link(self, amount_paise: int, *, customer: dict, description: str,
                            idempotency_key: str) -> dict:
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        self.calls.append(("create_payment_link", idempotency_key))
        if self.live:
            resp = self._live_link({
                "amount": amount_paise, "currency": "INR", "description": description,
                "customer": {"name": customer.get("name", ""),
                             "email": customer.get("email", ""),
                             "contact": customer.get("contact", "")},
                "notify": {"sms": False, "email": False},  # we simulate delivery
                "reference_id": idempotency_key,
            })
        else:
            fid = _fake_id("plink_", idempotency_key)
            resp = {"id": fid, "entity": "payment_link", "amount": amount_paise,
                    "currency": "INR", "status": "created",
                    "short_url": "https://rzp.io/i/" + fid[6:14],
                    "description": description, "reference_id": idempotency_key}
        return self._remember(idempotency_key, resp)


def make_gateway() -> RazorpayGateway:
    # Fail safe: live calls need an explicit RAZORPAY_LIVE=1 plus both keys plus
    # dry-run off. Anything else -> the local fake.
    if not config.razorpay_is_live():
        return RazorpayGateway(None, live=False)
    import razorpay
    client = razorpay.Client(auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
    return RazorpayGateway(client, live=True, store_path=config.IDEMPOTENCY_STORE)


# --------------------------------------------------------------------------- #
# The outcome oracle - rolls the case's hidden latent params. Executor only.
# --------------------------------------------------------------------------- #

def _near_day(dt: datetime, day: int) -> bool:
    """True if dt's day-of-month is within one day of `day`, month wrap included."""
    d = dt.day
    if abs(d - day) <= 1:
        return True
    return (d in (1, 2) and day in (28, 29, 30, 31)) or (day in (1, 2) and d in (28, 29, 30, 31))


def _recovered(case: dict, action: str, clock: Clock, rng: random.Random) -> bool:
    L = case["latent"]
    if L["chronic"]:
        return rng.random() < 0.02

    if action in config.RETRY_INTERVENTIONS or action == "switch_method":
        if L["method_dead"]:
            return rng.random() < L["base_recovery_prob"]
        p = L["base_recovery_prob"]
        if action == "retry_now":
            p = max(p, L["transient_retry_prob"])
        if L["funds_return_day"] is not None and _near_day(clock.now(), L["funds_return_day"]):
            p = max(p, L["timing_bonus_prob"])
        if L["limit_resets"]:
            p = max(p, 0.60)
        return rng.random() < p

    if action == "send_payment_link":
        if not case["customer"]["reachable"]:
            return False
        return rng.random() < L["link_response_prob"]

    if action == "send_mandate_link":
        if not case["customer"]["reachable"]:
            return False
        return rng.random() < L["mandate_link_prob"]

    return False  # send_reminder / handoff_human / do_nothing never recover money directly


def _customer_reply(case: dict, rng: random.Random) -> str:
    p_stop = case["latent"].get("opt_out_prob", 0.02)
    r = rng.random()
    if r < p_stop:
        return rng.choice(["stop", "unsubscribe"])
    if r < p_stop + 0.01:
        return "dispute"
    return ""


def _cause_shift(case: dict, actions_taken: int) -> str:
    """A materially different cause revealed as the agent works the case, or ''.
    Today the simulator only models the subscription mandate dying mid-sequence:
    it surfaces on the Nth action, whatever that action is."""
    rev = case["latent"].get("mandate_revokes_at_attempt")
    if rev is not None and actions_taken + 1 == rev:
        return "mandate_cancelled"
    return ""


# --------------------------------------------------------------------------- #
# Guardrail timing (minimal here; step 7 formalises violation tracking)
# --------------------------------------------------------------------------- #

def _in_contact_window(dt: datetime) -> bool:
    return G.contact_window_start_hour <= dt.hour < G.contact_window_end_hour


def _to_contact_window(dt: datetime) -> datetime:
    if dt.hour < G.contact_window_start_hour:
        return dt.replace(hour=G.contact_window_start_hour, minute=0, second=0, microsecond=0)
    nxt = (dt + timedelta(days=1)).replace(
        hour=G.contact_window_start_hour, minute=0, second=0, microsecond=0)
    return nxt


def _guarded_time(action: str, scheduled_at: datetime, attempts: list) -> datetime:
    t = scheduled_at
    if action in config.RETRY_INTERVENTIONS:
        last = max((a.at for a in attempts if a.action in config.RETRY_INTERVENTIONS),
                   default=None)
        if last is not None:
            t = max(t, last + timedelta(hours=G.min_hours_between_attempts))
    if action in config.MESSAGE_INTERVENTIONS and not _in_contact_window(t):
        t = _to_contact_window(t)
    return t


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

@dataclass
class Attempt:
    index: int
    action: str
    scheduled_at: datetime
    at: datetime
    outcome: str = ""
    ref: str = ""
    idempotency_key: str = ""
    detail: str = ""


@dataclass
class ExecutionResult:
    case_id: str
    root_cause: str
    replanned: bool
    recovered: bool
    amount_recovered_paise: int
    stop_reason: str
    retries_used: int
    messages_sent: int
    attempts: list = field(default_factory=list)
    finished_at: datetime = None


def _idem(case_id: str, index: int, action: str) -> str:
    return f"dun_{case_id}_{index}_{action}"


# --------------------------------------------------------------------------- #
# execute_plan
# --------------------------------------------------------------------------- #

def execute_plan(case: dict, a_plan: policy.Plan, *, seed: int = None,
                 gateway: RazorpayGateway = None, clock: Clock = None) -> ExecutionResult:
    if seed is None:
        seed = config.RANDOM_SEED
    gateway = gateway or make_gateway()
    clock = clock or Clock(datetime.fromisoformat(case["failed_at"]))
    out_rng = random.Random(f"{seed}:{case['case_id']}:outcome")
    reply_rng = random.Random(f"{seed}:{case['case_id']}:reply")

    attempts: list = []
    retries_used = messages_sent = 0
    replanned = False
    current_cause = a_plan.root_cause
    replans_left = 1 if a_plan.replan_allowed else 0

    def finish(stop_reason: str, recovered: bool) -> ExecutionResult:
        amt = case["amount_paise"] if recovered else 0
        return ExecutionResult(case["case_id"], current_cause, replanned, recovered, amt,
                               stop_reason, retries_used, messages_sent, attempts, clock.now())

    timeline = deque(policy.schedule(a_plan, clock.now()))

    while timeline:
        step, scheduled_at = timeline.popleft()

        if len(attempts) >= G.max_attempts_hard_cap:
            attempts.append(Attempt(len(attempts), "handoff_human", scheduled_at, clock.now(),
                                    outcome="escalated", detail="Hard action cap reached."))
            return finish("escalated_to_human", False)

        at = _guarded_time(step.action, scheduled_at, attempts)
        clock.advance_to(at)
        idx = len(attempts)
        key = _idem(case["case_id"], idx, step.action)

        # --- the mandate died mid-sequence: re-plan once, or stop safely ---
        if _cause_shift(case, len(attempts)) == "mandate_cancelled" \
                and current_cause != "mandate_cancelled":
            if replans_left and a_plan.replan_allowed:
                replans_left -= 1
                replanned = True
                current_cause = "mandate_cancelled"
                attempts.append(Attempt(idx, step.action, scheduled_at, clock.now(),
                                        outcome="replanned",
                                        detail="subscription.halted mid-sequence: mandate revoked "
                                               "-> re-planned to mandate repair."))
                a_plan = policy.plan("mandate_cancelled", policy.context_from_case(case))
                timeline = deque(policy.schedule(a_plan, clock.now()))
                continue
            attempts.append(Attempt(idx, step.action, scheduled_at, clock.now(),
                                    outcome="mandate_dead",
                                    detail="Mandate revoked mid-sequence; going no further "
                                           "would breach its terms."))
            return finish("mandate_dead", False)

        # --- money-safety rail: never act on an out-of-bounds amount ---
        if step.action in config.RETRY_INTERVENTIONS or step.action in (
                "switch_method", "send_payment_link", "send_mandate_link"):
            if not _amount_ok(case["amount_paise"]):
                attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                        outcome="blocked",
                                        detail=f"amount {case['amount_paise']} paise outside "
                                               f"safety limits - escalated."))
                return finish("escalated_to_human", False)

        # --- retries ---
        if step.action in config.RETRY_INTERVENTIONS or step.action == "switch_method":
            if retries_used >= G.max_retries:
                attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                        outcome="skipped", detail="Retry cap reached."))
                continue
            try:
                order = gateway.create_order(case["amount_paise"],
                                             {"case_id": case["case_id"], "purpose": "dunning_retry"},
                                             key)
            except Exception as exc:  # a live API failure must not crash the batch
                attempts.append(Attempt(idx, step.action, scheduled_at, at, outcome="gateway_error",
                                        idempotency_key=key,
                                        detail=redact.sanitize(f"{type(exc).__name__}: {exc}")))
                return finish("escalated_to_human", False)
            retries_used += 1
            ok = _recovered(case, step.action, clock, out_rng)
            attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                    outcome="recovered" if ok else "failed",
                                    ref=order["id"], idempotency_key=key))
            if ok:
                return finish("recovered", True)
            continue

        # --- messages that carry a link ---
        if step.action in ("send_payment_link", "send_mandate_link"):
            if messages_sent >= G.max_messages_per_customer:
                attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                        outcome="skipped", detail="Message cap reached."))
                continue
            try:
                link = gateway.create_payment_link(
                    case["amount_paise"], customer=case["customer"],
                    description=("Re-authorise your subscription" if step.action == "send_mandate_link"
                                 else "Complete your payment"),
                    idempotency_key=key)
            except Exception as exc:
                attempts.append(Attempt(idx, step.action, scheduled_at, at, outcome="gateway_error",
                                        idempotency_key=key,
                                        detail=redact.sanitize(f"{type(exc).__name__}: {exc}")))
                return finish("escalated_to_human", False)
            messages_sent += 1
            reply = _customer_reply(case, reply_rng)
            if reply:
                attempts.append(Attempt(idx, step.action, scheduled_at, at, ref=link["id"],
                                        idempotency_key=key, outcome="customer_opted_out",
                                        detail=f"customer replied {reply!r}"))
                return finish("customer_opted_out", False)
            ok = _recovered(case, step.action, clock, out_rng)
            attempts.append(Attempt(idx, step.action, scheduled_at, at, ref=link["id"],
                                    idempotency_key=key,
                                    outcome="recovered" if ok else "sent"))
            if ok:
                return finish("recovered", True)
            continue

        # --- reminder (message only) ---
        if step.action == "send_reminder":
            if messages_sent >= G.max_messages_per_customer:
                attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                        outcome="skipped", detail="Message cap reached."))
                continue
            msg = llm.write_message(customer_name=case["customer"]["name"],
                                    amount_rupees=case["amount_rupees"],
                                    language=case["customer"]["language"], ask="retry")
            messages_sent += 1
            reply = _customer_reply(case, reply_rng)
            if reply:
                attempts.append(Attempt(idx, step.action, scheduled_at, at,
                                        outcome="customer_opted_out",
                                        detail=f"customer replied {reply!r}"))
                return finish("customer_opted_out", False)
            attempts.append(Attempt(idx, step.action, scheduled_at, at, outcome="sent",
                                    detail=msg.text[:90]))
            continue

        # --- terminal steps ---
        if step.action == "handoff_human":
            attempts.append(Attempt(idx, step.action, scheduled_at, at, outcome="escalated",
                                    detail=a_plan.rationale[:140]))
            return finish("escalated_to_human", False)

        if step.action == "do_nothing":
            attempts.append(Attempt(idx, step.action, scheduled_at, at, outcome="noop",
                                    detail="Deliberately written off."))
            return finish("written_off", False)

    return finish("max_retries_reached", False)


# --------------------------------------------------------------------------- #
# Batch + CLI
# --------------------------------------------------------------------------- #

def run_case(case: dict, event: dict, *, seed: int = None,
             gateway: RazorpayGateway = None) -> ExecutionResult:
    dx = diagnose.diagnose(event)
    a_plan = policy.plan(dx.root_cause, policy.context_from_case(case))
    return execute_plan(case, a_plan, seed=seed, gateway=gateway)


def result_to_dict(r: ExecutionResult) -> dict:
    return {
        "case_id": r.case_id, "root_cause": r.root_cause, "replanned": r.replanned,
        "recovered": r.recovered, "amount_recovered_paise": r.amount_recovered_paise,
        "stop_reason": r.stop_reason, "retries_used": r.retries_used,
        "messages_sent": r.messages_sent,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "attempts": [
            {"index": a.index, "action": a.action, "outcome": a.outcome,
             "scheduled_at": a.scheduled_at.isoformat(), "at": a.at.isoformat(),
             "ref": a.ref, "idempotency_key": a.idempotency_key, "detail": a.detail}
            for a in r.attempts
        ],
    }


def _violations(results) -> int:
    bad = 0
    for r in results:
        if r.retries_used > G.max_retries:
            bad += 1
        if r.messages_sent > G.max_messages_per_customer:
            bad += 1
        retry_times = [a.at for a in r.attempts if a.action in config.RETRY_INTERVENTIONS]
        for earlier, later in zip(retry_times, retry_times[1:]):
            if (later - earlier) < timedelta(hours=G.min_hours_between_attempts):
                bad += 1
        for a in r.attempts:
            if a.action in config.MESSAGE_INTERVENTIONS and not _in_contact_window(a.at):
                bad += 1
    return bad


def _print_summary(results, cases_by_id) -> None:
    n = len(results)
    recovered = [r for r in results if r.recovered]
    at_risk = sum(cases_by_id[r.case_id]["amount_paise"] for r in results)
    got = sum(r.amount_recovered_paise for r in results)
    stops = Counter(r.stop_reason for r in results)
    attempts_total = sum(len(r.attempts) for r in results)

    print(f"\nExecuted {n} cases\n")
    print(f"  recovered            : {len(recovered)}/{n}  ({len(recovered) / n:.1%})")
    print(f"  value recovered      : Rs {got // 100:,} of Rs {at_risk // 100:,}  "
          f"({got / at_risk:.1%})")
    print(f"  avg attempts / case  : {attempts_total / n:.2f}")
    print(f"  re-planned mid-run   : {sum(r.replanned for r in results)}")
    print(f"  guardrail violations : {_violations(results)}")
    print("\n  stop reason:")
    for k, v in stops.most_common():
        print(f"    {k:<22} {v:>4}  ({v / n:.0%})")
    print(f"\n  Razorpay mode        : {'LIVE test-mode' if make_gateway().live else 'local fake'}\n")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Diagnose, plan and execute every event.")
    parser.add_argument("--events", type=Path, default=config.EVENTS_FILE)
    parser.add_argument("--cases", type=Path, default=config.CASES_FILE)
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    parser.add_argument("--dump", type=Path, default=None)
    args = parser.parse_args(argv)

    events = feed.load_events(args.events)
    cases_by_id = {c["case_id"]: c for c in feed.load_cases(args.cases)}
    gateway = make_gateway()

    results = []
    quarantined = 0
    for event in events:
        if not feed.verify_signature(event):      # reject unsigned / tampered events
            quarantined += 1
            continue
        case = cases_by_id[event["case_id"]]
        dx = diagnose.diagnose(event)
        a_plan = policy.plan(dx.root_cause, policy.context_from_case(case))
        results.append(execute_plan(case, a_plan, seed=args.seed, gateway=gateway))
    if quarantined:
        print(f"  WARNING: {quarantined} events failed signature verification (skipped)")

    if args.dump:
        with Path(args.dump).open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(result_to_dict(r), ensure_ascii=False) + "\n")

    _print_summary(results, cases_by_id)


if __name__ == "__main__":
    main()
