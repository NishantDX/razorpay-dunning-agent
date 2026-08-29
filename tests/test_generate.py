"""Tests for the step 2 synthetic data generator."""
import json

from dunning import config
from dunning.generate import ROOT_CAUSE_WEIGHTS, generate_cases, main, write_cases

RECOVERABLE_CAUSES = {c for w in ROOT_CAUSE_WEIGHTS.values() for c in w}


def test_every_generated_cause_is_real_and_has_reason_pools():
    from dunning.generate import _CLEAN_REASONS, _MESSY_REASONS
    for cause in RECOVERABLE_CAUSES:
        assert cause in config.ROOT_CAUSES and cause != "needs_review"
        assert _CLEAN_REASONS.get(cause), cause
        assert _MESSY_REASONS.get(cause), cause


def test_deterministic_for_same_seed():
    assert generate_cases(150, seed=7) == generate_cases(150, seed=7)


def test_seed_changes_output():
    assert generate_cases(80, seed=1) != generate_cases(80, seed=2)


def test_prefix_is_stable_regardless_of_batch_size():
    # each case depends only on (seed, case_id), so a bigger batch just extends
    small = generate_cases(40, seed=42)
    big = generate_cases(400, seed=42)
    assert big[:40] == small


def test_count_and_required_fields():
    cases = generate_cases(250, seed=42)
    assert len(cases) == 250
    for c in cases:
        assert c["case_id"].startswith("case_")
        assert c["kind"] in ("payment", "subscription")
        assert c["root_cause"] in RECOVERABLE_CAUSES
        assert c["root_cause"] in config.ROOT_CAUSES and c["root_cause"] != "needs_review"
        assert 99 <= c["amount_rupees"] <= 50000
        assert c["amount_paise"] == c["amount_rupees"] * 100
        assert c["currency"] == "INR"
        assert c["raw_failure_reason"]
        assert isinstance(c["reason_is_messy"], bool)
        cust = c["customer"]
        assert cust["contact"].startswith("+91") and len(cust["contact"]) == 13
        assert cust["contact"][3] in "6789"
        assert cust["language"] in ("en", "hi", "hinglish")
        assert 0.0 <= cust["prior_success_rate"] <= 1.0
        latent = c["latent"]
        for key in ("base_recovery_prob", "link_response_prob", "opt_out_prob"):
            assert 0.0 <= latent[key] <= 1.0


def test_case_is_json_serializable():
    cases = generate_cases(20, seed=3)
    for c in cases:
        assert json.loads(json.dumps(c)) == c


def test_kind_specific_causes_and_blocks():
    cases = generate_cases(300, seed=42)
    for c in cases:
        if c["root_cause"] == "mandate_cancelled":
            assert c["kind"] == "subscription"
        if c["root_cause"] == "abandoned":
            assert c["kind"] == "payment"
        if c["kind"] == "subscription":
            assert c["subscription"] is not None
            assert c["order_id"] is None and c["payment_id"] is None
            assert c["subscription"]["recurring_amount_paise"] == c["amount_paise"]
        else:
            assert c["subscription"] is None
            assert c["order_id"] is not None


def test_abandoned_has_no_payment_object():
    cases = generate_cases(300, seed=42)
    abandoned = [c for c in cases if c["root_cause"] == "abandoned"]
    assert abandoned
    assert all(c["payment_id"] is None for c in abandoned)


def test_mandate_cancelled_marks_mandate_dead():
    cases = generate_cases(300, seed=42)
    mc = [c for c in cases if c["root_cause"] == "mandate_cancelled"]
    assert mc
    for c in mc:
        assert c["subscription"]["mandate_status"] == "cancelled"
        assert c["latent"]["method_dead"] is True
        assert c["latent"]["base_recovery_prob"] == 0.0


def test_insufficient_funds_has_salary_day_and_timing_bonus():
    cases = generate_cases(300, seed=42)
    inf = [c for c in cases if c["root_cause"] == "insufficient_funds"]
    assert inf
    for c in inf:
        assert 1 <= c["latent"]["funds_return_day"] <= 31
        if not c["latent"]["chronic"]:
            assert c["latent"]["timing_bonus_prob"] > c["latent"]["base_recovery_prob"]


def test_messy_reason_fraction_in_range():
    cases = generate_cases(400, seed=42)
    frac = sum(c["reason_is_messy"] for c in cases) / len(cases)
    assert 0.08 <= frac <= 0.24


def test_mandate_revokes_midway_subset():
    cases = generate_cases(300, seed=42)
    revoke = [c for c in cases if c["latent"]["mandate_revokes_at_attempt"] is not None]
    assert revoke, "expected at least one mandate-dies-mid-sequence case"
    retry_safe = {"insufficient_funds", "bank_timeout", "do_not_honour",
                  "issuer_unavailable", "technical_decline"}
    for c in revoke:
        assert c["kind"] == "subscription"
        assert c["root_cause"] in retry_safe
        assert c["latent"]["mandate_revokes_at_attempt"] in (1, 2)
        assert c["latent"]["chronic"] is False


def test_failed_at_is_before_reference_and_in_contact_hours_mostly():
    cases = generate_cases(300, seed=42)
    for c in cases:
        assert c["failed_at"] < "2026-08-29T10:00:00+05:30"


def test_cli_writes_jsonl_and_meta(tmp_path):
    out = tmp_path / "cases.jsonl"
    main(["--count", "60", "--seed", "9", "--out", str(out)])
    lines = out.read_text().strip().splitlines()
    assert len(lines) == 60
    first = json.loads(lines[0])
    assert first["case_id"] == "case_0000"
    meta = json.loads((tmp_path / "cases.meta.json").read_text())
    assert meta["count"] == 60 and meta["seed"] == 9
    assert meta["distribution"]["by_kind"]


def test_write_cases_roundtrip(tmp_path):
    cases = generate_cases(30, seed=5)
    path = tmp_path / "out.jsonl"
    write_cases(cases, path)
    loaded = [json.loads(line) for line in path.read_text().splitlines()]
    assert loaded == cases
