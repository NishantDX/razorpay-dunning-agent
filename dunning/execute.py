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

from dunning import audit, config, diagnose, feed, guardrails, messaging, policy, redact

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
        self.dedupe_hits = 0       # repeated idempotency key -> a prevented double action
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
            self.dedupe_hits += 1
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
            self.dedupe_hits += 1
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


def _already_paid(case: dict, actions_taken: int) -> bool:
    """A status check the agent runs BEFORE every charge / link create. In live
    mode this is client.order.fetch / client.payment.all; here it consults the
    simulator, which only returns True for a planted out-of-band payment."""
    at = case["latent"].get("paid_out_of_band_at_action")
    return at is not None and actions_taken + 1 == at


# All retry-spacing, contact-window and cap logic now lives in dunning.guardrails.


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #

_MSG_ASK = {
    "send_reminder": "retry",
    "send_payment_link": "pay_link",
    "send_mandate_link": "reauthorise_mandate",
}


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
    messages: dict = field(default_factory=dict)   # channel -> body (for the audit log)


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
    double_charge_prevented: bool = False


def _idem(case_id: str, index: int, action: str) -> str:
    return f"dun_{case_id}_{index}_{action}"


def _compose_messages(case: dict, action: str, link_url: str = "") -> tuple:
    """Returns (messages_by_channel, short_summary_for_the_attempt_detail)."""
    msgs = messaging.compose(customer_name=case["customer"]["name"],
                             amount_rupees=case["amount_rupees"],
                             language=case["customer"]["language"],
                             ask=_MSG_ASK.get(action, "retry"), link_url=link_url)
    bodies = {ch: m.body for ch, m in msgs.items()}
    tmpl = any(m.from_template for m in msgs.values())
    summary = redact.sanitize(bodies["sms"][:90]) + (" [template]" if tmpl else "")
    return bodies, summary


# --------------------------------------------------------------------------- #
# execute_plan
# --------------------------------------------------------------------------- #

def execute_plan(case: dict, a_plan: policy.Plan, *, seed: int = None,
                 gateway: RazorpayGateway = None, clock: Clock = None,
                 ledger: guardrails.GuardrailLedger = None,
                 governor: guardrails.SpendGovernor = None) -> ExecutionResult:
    if seed is None:
        seed = config.RANDOM_SEED
    gateway = gateway or make_gateway()
    clock = clock or Clock(datetime.fromisoformat(case["failed_at"]))
    ledger = ledger or guardrails.GuardrailLedger()
    out_rng = random.Random(f"{seed}:{case['case_id']}:outcome")
    reply_rng = random.Random(f"{seed}:{case['case_id']}:reply")

    attempts: list = []
    replanned = False
    dcp = False
    current_cause = a_plan.root_cause
    replans_left = 1 if a_plan.replan_allowed else 0

    def finish(stop_reason: str, recovered: bool) -> ExecutionResult:
        amt = case["amount_paise"] if recovered else 0
        return ExecutionResult(case["case_id"], current_cause, replanned, recovered, amt,
                               stop_reason, ledger.retries_used, ledger.messages_sent,
                               attempts, clock.now(), dcp)

    def add(action, scheduled_at, at, **kw):
        attempts.append(Attempt(len(attempts), action, scheduled_at, at, **kw))

    timeline = deque(policy.schedule(a_plan, clock.now()))

    while timeline:
        step, scheduled_at = timeline.popleft()

        # --- the mandate died mid-sequence: re-plan once, or stop safely ---
        if _cause_shift(case, ledger.actions_taken) == "mandate_cancelled" \
                and current_cause != "mandate_cancelled":
            if replans_left and a_plan.replan_allowed:
                replans_left -= 1
                replanned = True
                current_cause = "mandate_cancelled"
                add(step.action, scheduled_at, clock.now(), outcome="replanned",
                    detail="subscription.halted mid-sequence: mandate revoked -> "
                           "re-planned to mandate repair.")
                a_plan = policy.plan("mandate_cancelled", policy.context_from_case(case))
                timeline = deque(policy.schedule(a_plan, clock.now()))
                continue
            add(step.action, scheduled_at, clock.now(), outcome="mandate_dead",
                detail="Mandate revoked mid-sequence; going no further would breach its terms.")
            return finish("mandate_dead", False)

        # --- guardrail: allow / defer / skip / halt, all decided in one place ---
        decision = ledger.evaluate(step.action, scheduled_at)
        if decision.kind == "halt":
            add(step.action, scheduled_at, clock.now(), outcome="escalated",
                detail=f"{decision.rule}: {decision.note}")
            return finish("escalated_to_human", False)
        if decision.kind == "skip":
            add(step.action, scheduled_at, scheduled_at, outcome="skipped",
                detail=f"{decision.rule}: {decision.note}")
            continue
        at = decision.at
        clock.advance_to(at)
        key = _idem(case["case_id"], len(attempts), step.action)
        defer_note = "" if decision.kind == "allow" else f"deferred by {decision.rule}"

        # --- status check before any charge / link: is it already paid? ---
        if step.action in config.RETRY_INTERVENTIONS or step.action in (
                "switch_method", "send_payment_link", "send_mandate_link"):
            if _already_paid(case, ledger.actions_taken):
                dcp = True
                add(step.action, scheduled_at, at, outcome="recovered",
                    detail="status check found the payment already completed out of band "
                           "- no new charge created (double charge prevented).")
                return finish("recovered", True)

        # --- money-safety rails ---
        if step.action in config.RETRY_INTERVENTIONS or step.action in (
                "switch_method", "send_payment_link", "send_mandate_link"):
            if not _amount_ok(case["amount_paise"]):
                add(step.action, scheduled_at, at, outcome="blocked",
                    detail=f"amount {case['amount_paise']} paise outside safety limits.")
                return finish("escalated_to_human", False)
            # the run-wide ceiling governs auto-charges (retries), not links the
            # customer must act on
            if step.action in config.RETRY_INTERVENTIONS and governor is not None \
                    and not governor.may_attempt(case["amount_paise"]):
                add(step.action, scheduled_at, at, outcome="blocked",
                    detail="run spend ceiling reached - escalated.")
                return finish("escalated_to_human", False)

        # --- retries ---
        if step.action in config.RETRY_INTERVENTIONS or step.action == "switch_method":
            try:
                order = gateway.create_order(
                    case["amount_paise"],
                    {"case_id": case["case_id"], "purpose": "dunning_retry"}, key)
            except Exception as exc:  # a live API failure must not crash the batch
                if governor is not None:
                    governor.note_gateway_error()
                add(step.action, scheduled_at, at, outcome="gateway_error", idempotency_key=key,
                    detail=redact.sanitize(f"{type(exc).__name__}: {exc}"))
                return finish("escalated_to_human", False)
            ledger.record(step.action, at)
            if governor is not None:
                governor.note_attempt(case["amount_paise"])
            ok = _recovered(case, step.action, clock, out_rng)
            add(step.action, scheduled_at, at, outcome="recovered" if ok else "failed",
                ref=order["id"], idempotency_key=key, detail=defer_note)
            if ok:
                return finish("recovered", True)
            continue

        # --- messages that carry a link ---
        if step.action in ("send_payment_link", "send_mandate_link"):
            try:
                link = gateway.create_payment_link(
                    case["amount_paise"], customer=case["customer"],
                    description=("Re-authorise your subscription"
                                 if step.action == "send_mandate_link" else "Complete your payment"),
                    idempotency_key=key)
            except Exception as exc:
                if governor is not None:
                    governor.note_gateway_error()
                add(step.action, scheduled_at, at, outcome="gateway_error", idempotency_key=key,
                    detail=redact.sanitize(f"{type(exc).__name__}: {exc}"))
                return finish("escalated_to_human", False)
            ledger.record(step.action, at)
            bodies, summary = _compose_messages(case, step.action, link["short_url"])
            reply = _customer_reply(case, reply_rng)
            if reply:
                add(step.action, scheduled_at, at, ref=link["id"], idempotency_key=key,
                    outcome="customer_opted_out", detail=f"customer replied {reply!r}",
                    messages=bodies)
                return finish("customer_opted_out", False)
            ok = _recovered(case, step.action, clock, out_rng)
            add(step.action, scheduled_at, at, ref=link["id"], idempotency_key=key,
                outcome="recovered" if ok else "sent",
                detail=(defer_note + " " + summary).strip(), messages=bodies)
            if ok:
                return finish("recovered", True)
            continue

        # --- reminder (message only) ---
        if step.action == "send_reminder":
            ledger.record(step.action, at)
            bodies, summary = _compose_messages(case, step.action)
            reply = _customer_reply(case, reply_rng)
            if reply:
                add(step.action, scheduled_at, at, outcome="customer_opted_out",
                    detail=f"customer replied {reply!r}", messages=bodies)
                return finish("customer_opted_out", False)
            add(step.action, scheduled_at, at, outcome="sent", detail=summary, messages=bodies)
            continue

        # --- terminal steps ---
        if step.action == "handoff_human":
            add(step.action, scheduled_at, at, outcome="escalated", detail=a_plan.rationale[:140])
            return finish("escalated_to_human", False)

        if step.action == "do_nothing":
            add(step.action, scheduled_at, at, outcome="noop", detail="Deliberately written off.")
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
        "double_charge_prevented": r.double_charge_prevented,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "attempts": [
            {"index": a.index, "action": a.action, "outcome": a.outcome,
             "scheduled_at": a.scheduled_at.isoformat(), "at": a.at.isoformat(),
             "ref": a.ref, "idempotency_key": a.idempotency_key, "detail": a.detail,
             "messages": a.messages}
            for a in r.attempts
        ],
    }


_violations = guardrails.count_violations  # kept name for callers/tests


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
    print(f"  guardrail violations : {guardrails.count_violations(results)}")
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
    governor = guardrails.SpendGovernor()

    results = []
    quarantined = 0
    sink = audit.AuditSink().open(seed=args.seed)
    for event in events:
        if not feed.verify_signature(event):      # reject unsigned / tampered events
            quarantined += 1
            continue
        if governor.tripped():
            print(f"  HALTED: {governor.tripped()} - stopped launching new cases")
            break
        case = cases_by_id[event["case_id"]]
        dx = diagnose.diagnose(event)
        a_plan = policy.plan(dx.root_cause, policy.context_from_case(case))
        r = execute_plan(case, a_plan, seed=args.seed, gateway=gateway, governor=governor)
        results.append(r)
        sink.record_case(case, dx, a_plan, r)
    manifest = sink.close()
    if quarantined:
        print(f"  WARNING: {quarantined} events failed signature verification (skipped)")

    guardrails.assert_no_violations(results)  # independent check - must pass
    print(f"  audit log            : {manifest['record_count']} records, "
          f"chain head {manifest['chain_head'][:12]}...")

    if args.dump:
        with Path(args.dump).open("w", encoding="utf-8") as fh:
            for r in results:
                fh.write(json.dumps(result_to_dict(r), ensure_ascii=False) + "\n")

    _print_summary(results, cases_by_id)


if __name__ == "__main__":
    main()
