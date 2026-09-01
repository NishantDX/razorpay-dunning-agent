"""Step 9 - the append-only, tamper-evident audit log.

Every run writes ``logs/audit.jsonl``: one JSON line per fact - a diagnosis, a
plan, each attempt, a per-case summary - in the order they happened. Two
integrity properties:

1. **Hash chain.** Each line carries ``prev_hash`` (the previous line's hash) and
   its own ``hash`` = sha256(the line without its hash field). Edit or drop any
   line and every later line's hashes stop matching - you cannot quietly alter
   one entry.
2. **Signed manifest.** ``logs/audit.manifest.json`` records the chain head, the
   line count, a fingerprint of the guardrail / policy config the run used, and
   an HMAC of all that under ``AUDIT_SECRET``. Publish the manifest and the whole
   log is verifiable.

PII never reaches the log: customer records are passed through
``redact.redact_customer`` and free text through ``redact.sanitize`` on the way
in.

``make verify-audit`` (``python -m dunning.audit verify``) recomputes the chain
and checks the manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dunning import config, redact

AUDIT_VERSION = 1
_GENESIS = "0" * 64


def _canon(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _hash(obj: dict) -> str:
    return hashlib.sha256(_canon(obj)).hexdigest()


def config_fingerprint() -> str:
    g = config.GUARDRAILS
    m = config.MONEY
    payload = {
        "guardrails": [g.max_retries, g.min_hours_between_attempts,
                       g.contact_window_start_hour, g.contact_window_end_hour,
                       g.max_messages_per_customer, g.max_attempts_hard_cap],
        "money": [m.max_single_action_paise, m.min_single_action_paise,
                  m.max_total_attempted_paise, m.max_gateway_errors],
        "root_causes": list(config.ROOT_CAUSES),
        "interventions": list(config.INTERVENTIONS),
        "stop_reasons": list(config.STOP_REASONS),
    }
    return _hash(payload)[:16]


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #

class AuditSink:
    def __init__(self, path: Path = None, manifest_path: Path = None, *,
                 now: datetime = None):
        self.path = Path(path or config.AUDIT_LOG)
        self.manifest_path = Path(manifest_path or config.AUDIT_MANIFEST)
        self._now = now
        self._fh = None
        self._prev = _GENESIS
        self._seq = 0
        self._started = None
        self._counts = {"diagnosis": 0, "plan": 0, "attempt": 0, "case_summary": 0}

    # -- lifecycle --
    def open(self, *, seed: int) -> "AuditSink":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")  # a run starts a fresh log
        self._prev = _GENESIS
        self._seq = 0
        self._started = self._ts()
        self._seed = seed
        self._fingerprint = config_fingerprint()
        self._append("run_start", {"audit_version": AUDIT_VERSION, "seed": seed,
                                   "config_fingerprint": self._fingerprint})
        return self

    def close(self) -> dict:
        self._append("run_end", {"records": self._seq})
        head = self._prev
        if self._fh:
            self._fh.close()
            self._fh = None
        manifest = {
            "audit_version": AUDIT_VERSION,
            "log_file": str(self.path),
            "started_at": self._started,
            "finished_at": self._ts(),
            "seed": self._seed,
            "config_fingerprint": self._fingerprint,
            "record_count": self._seq,
            "counts": dict(self._counts),
            "chain_head": head,
        }
        manifest["hmac"] = hmac.new(config.AUDIT_SECRET.encode("utf-8"),
                                    _canon(manifest), hashlib.sha256).hexdigest()
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._fh:
            self.close()

    # -- helpers --
    def _ts(self) -> str:
        return (self._now or datetime.now(timezone.utc)).isoformat()

    _RESERVED = ("seq", "ts", "kind", "prev_hash", "hash")

    def _append(self, kind: str, payload: dict) -> str:
        line = {k: v for k, v in payload.items() if k not in self._RESERVED}
        line.update(seq=self._seq, ts=self._ts(), kind=kind, prev_hash=self._prev)
        line["hash"] = _hash({k: v for k, v in line.items() if k != "hash"})
        self._fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        self._prev = line["hash"]
        self._seq += 1
        if kind in self._counts:
            self._counts[kind] += 1
        return line["hash"]

    # -- the one call the batch runner makes per case --
    def record_case(self, case: dict, diagnosis, plan, result) -> None:
        cid = case["case_id"]
        self._append("diagnosis", {
            "case_id": cid, "root_cause": diagnosis.root_cause,
            "confidence": round(diagnosis.confidence, 3), "stage": diagnosis.stage,
            "signal": redact.sanitize(diagnosis.signal)})
        self._append("plan", {
            "case_id": cid, "root_cause": plan.root_cause,
            "steps": [s.action for s in plan.steps],
            "rationale": plan.rationale, "replan_allowed": plan.replan_allowed,
            "adjustments": list(plan.adjustments)})
        for a in result.attempts:
            self._append("attempt", {
                "case_id": cid, "index": a.index, "action": a.action,
                "outcome": a.outcome, "scheduled_at": a.scheduled_at.isoformat(),
                "at": a.at.isoformat(), "ref": a.ref,
                "idempotency_key": a.idempotency_key,
                "detail": redact.sanitize(a.detail), "messages": a.messages})
        self._append("case_summary", {
            "case_id": cid, "customer": redact.redact_customer(case["customer"]),
            "case_kind": case["kind"], "amount_paise": case["amount_paise"],
            "recovered": result.recovered,
            "amount_recovered_paise": result.amount_recovered_paise,
            "stop_reason": result.stop_reason, "retries_used": result.retries_used,
            "messages_sent": result.messages_sent, "replanned": result.replanned,
            "double_charge_prevented": result.double_charge_prevented,
            "showcase": case["latent"].get("showcase")})


# --------------------------------------------------------------------------- #
# Verifier
# --------------------------------------------------------------------------- #

def verify(log_path: Path = None, manifest_path: Path = None) -> tuple:
    log_path = Path(log_path or config.AUDIT_LOG)
    manifest_path = Path(manifest_path or config.AUDIT_MANIFEST)
    problems = []

    if not log_path.exists():
        return False, [f"{log_path} not found"]
    if not manifest_path.exists():
        return False, [f"{manifest_path} not found"]

    lines = [json.loads(x) for x in log_path.read_text("utf-8").splitlines() if x.strip()]
    prev = _GENESIS
    for i, line in enumerate(lines):
        if line.get("prev_hash") != prev:
            problems.append(f"line {i} (seq {line.get('seq')}): prev_hash break")
        recomputed = _hash({k: v for k, v in line.items() if k != "hash"})
        if recomputed != line.get("hash"):
            problems.append(f"line {i} (seq {line.get('seq')}): hash mismatch")
        prev = line.get("hash")

    manifest = json.loads(manifest_path.read_text("utf-8"))
    given_hmac = manifest.get("hmac", "")
    expected = hmac.new(config.AUDIT_SECRET.encode("utf-8"),
                        _canon({k: v for k, v in manifest.items() if k != "hmac"}),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(given_hmac, expected):
        problems.append("manifest HMAC invalid (wrong AUDIT_SECRET or manifest edited)")
    if manifest.get("chain_head") != prev:
        problems.append("manifest chain_head does not match the log")
    if manifest.get("record_count") != len(lines):
        problems.append(f"record_count {manifest.get('record_count')} != {len(lines)} lines")
    if manifest.get("config_fingerprint") != config_fingerprint():
        problems.append("config fingerprint changed since the run (guardrails/policy differ)")

    return (not problems), problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify the audit log.")
    parser.add_argument("cmd", nargs="?", default="verify", choices=["verify"])
    parser.add_argument("--log", type=Path, default=config.AUDIT_LOG)
    parser.add_argument("--manifest", type=Path, default=config.AUDIT_MANIFEST)
    args = parser.parse_args(argv)

    ok, problems = verify(args.log, args.manifest)
    if ok:
        m = json.loads(Path(args.manifest).read_text("utf-8"))
        print(f"AUDIT VERIFIED  -  {m['record_count']} records, "
              f"chain head {m['chain_head'][:16]}..., fingerprint {m['config_fingerprint']}")
        return 0
    print("AUDIT VERIFICATION FAILED:")
    for p in problems:
        print(f"  - {p}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
