"""Tests for the step 10 batch runner and HTML report."""
import pytest

from dunning import audit, config, guardrails, report, run_batch


@pytest.fixture
def rr(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(config, "AUDIT_MANIFEST", tmp_path / "audit.manifest.json")
    return run_batch.run(seed=42, count=60, write_disk=False)


def test_run_produces_a_coherent_result(rr):
    assert rr.n == 60
    assert all(r.stop_reason in config.STOP_REASONS for r in rr.results)
    assert rr.recovered_paise() <= rr.at_risk_paise
    assert set(rr.baselines) == set(("naive_one_retry", "blind_three"))
    assert all(len(v) == rr.n for v in rr.baselines.values())


def test_run_is_deterministic_for_a_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "a.jsonl")
    monkeypatch.setattr(config, "AUDIT_MANIFEST", tmp_path / "a.m.json")
    a = run_batch.run(seed=42, count=50, write_disk=False)
    b = run_batch.run(seed=42, count=50, write_disk=False)
    assert [x.amount_recovered_paise for x in a.results] == \
           [x.amount_recovered_paise for x in b.results]


def test_run_writes_a_verifiable_audit_log(rr):
    ok, problems = audit.verify()
    assert ok, problems
    assert rr.audit_ok is True


def test_no_guardrail_violations_in_a_full_run(rr):
    assert guardrails.count_violations(rr.results) == 0


def test_agent_beats_the_naive_baseline(rr):
    agent = rr.recovered_paise()
    naive = rr.baseline_summary("naive_one_retry")["recovered_paise"]
    assert agent > naive


def test_both_deliberate_failures_are_showcased(rr):
    sc = rr.showcases()
    assert set(sc) == {"mandate_revoked_midway", "double_charge_prevented"}
    _cid, mandate_r = sc["mandate_revoked_midway"]
    assert mandate_r.replanned or mandate_r.stop_reason == "mandate_dead"
    _cid, dc_r = sc["double_charge_prevented"]
    assert dc_r.double_charge_prevented is True
    assert rr.double_charges_prevented() >= 1


def test_baseline_rule_break_estimate_is_reported(rr):
    b = rr.baseline_summary("blind_three")
    assert b["rule_breaks"] > b["recovered"]     # 3 blind retries -> lots of gap breaches


# --- report ---------------------------------------------------------- #

def test_report_context_is_sane(rr):
    ctx = report.build_context(rr)
    assert 0.0 <= ctx["recovered_pct"] <= 1.0
    assert ctx["violations"] == 0
    assert ctx["seed"] == 42
    assert len(ctx["strategies"]) == 3 and ctx["strategies"][-1]["is_agent"]
    assert ctx["causes"] and all(0 <= c["pct"] <= 1 for c in ctx["causes"])


def test_report_renders_valid_html(rr, tmp_path):
    out = report.write(rr, tmp_path / "latest.html")
    html = out.read_text()
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    assert "{{" not in html and "{%" not in html
    assert "run report" in html and "Agent vs" in html
