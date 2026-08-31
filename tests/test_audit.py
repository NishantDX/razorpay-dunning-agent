"""Tests for the step 9 tamper-evident audit log."""
import json
from datetime import datetime, timezone

import pytest

from dunning import audit, config, diagnose, policy
from dunning.execute import RazorpayGateway, execute_plan
from dunning.feed import build_events
from dunning.generate import generate_cases

FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def written(tmp_path):
    """Run a small batch through a sink; return (log_path, manifest_path, n_cases)."""
    log = tmp_path / "audit.jsonl"
    man = tmp_path / "audit.manifest.json"
    cases = generate_cases(25, seed=42)
    by_id = {c["case_id"]: c for c in cases}
    gw = RazorpayGateway(None, live=False)
    sink = audit.AuditSink(log, man, now=FIXED_NOW).open(seed=42)
    for e in build_events(cases):
        c = by_id[e["case_id"]]
        dx = diagnose.diagnose(e)
        p = policy.plan(dx.root_cause, policy.context_from_case(c))
        r = execute_plan(c, p, seed=42, gateway=gw)
        sink.record_case(c, dx, p, r)
    manifest = sink.close()
    return log, man, len(cases), manifest


def test_a_clean_log_verifies(written):
    log, man, n, manifest = written
    ok, problems = audit.verify(log, man)
    assert ok, problems
    assert manifest["counts"]["case_summary"] == n
    assert manifest["counts"]["diagnosis"] == n
    assert manifest["record_count"] == sum(1 for _ in log.read_text().splitlines())


def test_tampering_with_a_line_is_detected(written):
    log, man, _n, _m = written
    lines = log.read_text().splitlines()
    row = json.loads(lines[10])
    row["outcome"] = "recovered"       # forge a better result
    lines[10] = json.dumps(row)
    log.write_text("\n".join(lines) + "\n")
    ok, problems = audit.verify(log, man)
    assert not ok and any("hash mismatch" in p for p in problems)


def test_dropping_a_line_breaks_the_chain(written):
    log, man, _n, _m = written
    lines = log.read_text().splitlines()
    del lines[20]
    log.write_text("\n".join(lines) + "\n")
    ok, problems = audit.verify(log, man)
    assert not ok and any("prev_hash break" in p for p in problems)


def test_editing_the_manifest_is_detected(written):
    log, man, _n, _m = written
    m = json.loads(man.read_text())
    m["record_count"] = m["record_count"] + 5
    man.write_text(json.dumps(m))
    ok, problems = audit.verify(log, man)
    assert not ok
    assert any("HMAC" in p for p in problems)


def test_wrong_audit_secret_fails_hmac(written, monkeypatch):
    log, man, _n, _m = written
    monkeypatch.setattr(config, "AUDIT_SECRET", "a-different-secret")
    ok, problems = audit.verify(log, man)
    assert not ok and any("HMAC" in p for p in problems)


def test_config_change_since_run_is_flagged(written, monkeypatch):
    log, man, _n, _m = written
    hardened = config.Guardrails(max_retries=1)
    monkeypatch.setattr(config, "GUARDRAILS", hardened)
    ok, problems = audit.verify(log, man)
    assert not ok and any("fingerprint" in p for p in problems)


def test_no_raw_pii_in_the_log(written):
    log, _man, _n, _m = written
    cases = generate_cases(25, seed=42)
    blob = log.read_text()
    for c in cases:
        assert c["customer"]["email"] not in blob
        assert c["customer"]["contact"] not in blob
        # surname (second token of the name), when present, must not appear
        parts = c["customer"]["name"].split()
        if len(parts) > 1:
            assert parts[-1] not in blob


def test_record_case_emits_the_expected_kinds(tmp_path):
    log = tmp_path / "a.jsonl"
    man = tmp_path / "a.manifest.json"
    cases = generate_cases(1, seed=7)
    e = build_events(cases)[0]
    c = cases[0]
    dx = diagnose.diagnose(e)
    p = policy.plan(dx.root_cause, policy.context_from_case(c))
    r = execute_plan(c, p, seed=7, gateway=RazorpayGateway(None, live=False))
    sink = audit.AuditSink(log, man, now=FIXED_NOW).open(seed=7)
    sink.record_case(c, dx, p, r)
    sink.close()
    kinds = [json.loads(x)["kind"] for x in log.read_text().splitlines()]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert kinds.count("diagnosis") == 1 and kinds.count("plan") == 1
    assert kinds.count("case_summary") == 1
    assert kinds.count("attempt") == len(r.attempts)
