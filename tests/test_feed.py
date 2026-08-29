"""Tests for the step 3 event feed."""
import json

from dunning.feed import (
    EVENT_TYPES,
    build_event,
    build_events,
    load_events,
    main,
    replay,
    write_events,
)
from dunning.generate import generate_cases


def _cases(n=200, seed=42):
    return generate_cases(n, seed=seed)


def test_one_event_per_case():
    cases = _cases()
    assert len(build_events(cases)) == len(cases)


def test_event_type_matches_case_kind():
    for c in _cases(300):
        ev = build_event(c)
        assert ev["event"] in EVENT_TYPES
        if c["kind"] == "subscription":
            assert ev["event"] == "subscription.pending"
        elif c["root_cause"] == "abandoned":
            assert ev["event"] == "order.abandoned"
        else:
            assert ev["event"] == "payment.failed"


def test_events_sorted_by_time():
    events = build_events(_cases(300))
    stamps = [e["created_at"] for e in events]
    assert stamps == sorted(stamps)


def test_envelope_shape_is_webhook_like():
    events = build_events(_cases(120))
    for e in events:
        assert e["entity"] == "event"
        assert e["account_id"].startswith("acc_")
        assert isinstance(e["contains"], list) and e["contains"]
        assert isinstance(e["created_at"], int)
        assert isinstance(e["payload"], dict)
        for key in e["contains"]:
            assert key in e["payload"]
            assert "entity" in e["payload"][key]


def test_payment_entity_carries_case_fields():
    for c in _cases(300):
        e = build_event(c)
        if "payment" not in e["payload"]:
            continue
        ent = e["payload"]["payment"]["entity"]
        assert ent["amount"] == c["amount_paise"]
        assert ent["currency"] == "INR"
        assert ent["status"] == "failed"
        assert ent["error_description"] == c["raw_failure_reason"]
        assert ent["notes"]["case_id"] == c["case_id"]


def test_case_id_is_recoverable_from_every_event():
    cases = _cases(300)
    by_id = {c["case_id"] for c in cases}
    for e in build_events(cases):
        assert e["case_id"] in by_id
        # also stashed in notes, the way a real merchant links their records
        payload_notes = [
            v["entity"].get("notes", {}).get("case_id")
            for v in e["payload"].values()
        ]
        assert e["case_id"] in payload_notes


def test_latent_never_leaks_into_events():
    blob = json.dumps(build_events(_cases(300)))
    assert "latent" not in blob
    assert "base_recovery_prob" not in blob
    assert "funds_return_day" not in blob


def test_deterministic():
    cases = _cases()
    assert build_events(cases) == build_events(cases)


def test_replay_in_memory_matches_build():
    cases = _cases(150)
    assert list(replay(cases=cases)) == build_events(cases)


def test_write_and_load_roundtrip(tmp_path):
    events = build_events(_cases(80))
    path = tmp_path / "events.jsonl"
    write_events(events, path)
    assert load_events(path) == events
    assert list(replay(path)) == events


def test_cli_writes_events_file(tmp_path):
    cases_path = tmp_path / "cases.jsonl"
    out_path = tmp_path / "events.jsonl"
    from dunning.generate import main as gen_main

    gen_main(["--count", "50", "--seed", "9", "--out", str(cases_path)])
    main(["--cases", str(cases_path), "--out", str(out_path)])

    lines = out_path.read_text().strip().splitlines()
    assert len(lines) == 50
    first = json.loads(lines[0])
    assert first["entity"] == "event" and first["event"] in EVENT_TYPES
