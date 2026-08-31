"""Step 10 - the batch runner.

One command (`make run`) does the whole thing:

  generate cases -> sign into an event feed -> for each event:
  verify signature -> diagnose -> plan -> execute (real Razorpay test APIs) ->
  audit -> aggregate -> write reports/latest.html

It also runs the naive baseline over the *same* cases and seed, so the report
can show the agent's lift honestly. Single seed - results are deterministic for
it and it is printed on the report.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from dunning import (audit, baseline, config, diagnose, feed, generate,
                     guardrails, policy, report)
from dunning.execute import execute_plan, make_gateway, result_to_dict


@dataclass
class RunResult:
    seed: int
    n: int
    at_risk_paise: int
    results: list                      # list[ExecutionResult]
    diagnoses: dict                    # case_id -> Diagnosis
    plans: dict                       # case_id -> Plan
    cases_by_id: dict
    baselines: dict = field(default_factory=dict)   # strategy -> list[BaselineResult]
    gateway_calls: int = 0
    dedupe_hits: int = 0
    quarantined: int = 0
    audit_manifest: dict = field(default_factory=dict)
    audit_ok: bool = False

    # -- headline helpers --
    def recovered(self):
        return [r for r in self.results if r.recovered]

    def recovered_paise(self):
        return sum(r.amount_recovered_paise for r in self.results)

    def by_cause(self):
        tot, rec = Counter(), Counter()
        for r in self.results:
            tot[r.root_cause] += 1
            if r.recovered:
                rec[r.root_cause] += 1
        return tot, rec

    def stop_reasons(self):
        return Counter(r.stop_reason for r in self.results)

    def diagnoser_stages(self):
        return Counter(d.stage for d in self.diagnoses.values())

    def baseline_summary(self, strategy):
        rows = self.baselines.get(strategy, [])
        rec = [b for b in rows if b.recovered]
        return {
            "recovered": len(rec),
            "recovered_paise": sum(b.amount_recovered_paise for b in rows),
            "avg_attempts": (sum(b.attempts for b in rows) / len(rows)) if rows else 0,
        }


def run(seed: int = None, count: int = None, *, write_disk: bool = True) -> RunResult:
    seed = seed if seed is not None else config.RANDOM_SEED
    count = count if count is not None else config.BATCH_SIZE

    cases = generate.generate_cases(count, seed=seed)
    events = feed.build_events(cases)
    cases_by_id = {c["case_id"]: c for c in cases}

    if write_disk:
        generate.write_cases(cases, config.CASES_FILE)
        feed.write_events(events, config.EVENTS_FILE)

    gateway = make_gateway()
    governor = guardrails.SpendGovernor()
    sink = audit.AuditSink().open(seed=seed)

    results, diagnoses, plans = [], {}, {}
    quarantined = 0
    for event in events:
        if not feed.verify_signature(event):
            quarantined += 1
            continue
        if governor.tripped():
            break
        case = cases_by_id[event["case_id"]]
        dx = diagnose.diagnose(event)
        pl = policy.plan(dx.root_cause, policy.context_from_case(case))
        r = execute_plan(case, pl, seed=seed, gateway=gateway, governor=governor)
        results.append(r)
        diagnoses[case["case_id"]] = dx
        plans[case["case_id"]] = pl
        sink.record_case(case, dx, pl, r)
    manifest = sink.close()

    guardrails.assert_no_violations(results)

    rr = RunResult(
        seed=seed, n=len(results),
        at_risk_paise=sum(cases_by_id[r.case_id]["amount_paise"] for r in results),
        results=results, diagnoses=diagnoses, plans=plans, cases_by_id=cases_by_id,
        baselines={name: [fn(cases_by_id[r.case_id], seed) for r in results]
                   for name, fn in baseline.STRATEGIES.items()},
        gateway_calls=len(gateway.calls), dedupe_hits=gateway.dedupe_hits,
        quarantined=quarantined, audit_manifest=manifest,
    )
    ok, _ = audit.verify()
    rr.audit_ok = ok
    return rr


def _print_headline(rr: RunResult) -> None:
    got, risk = rr.recovered_paise(), rr.at_risk_paise
    print(f"\n  cases                : {rr.n}")
    print(f"  recovered            : {len(rr.recovered())}  ({len(rr.recovered()) / rr.n:.1%})")
    print(f"  value recovered      : Rs {got // 100:,} of Rs {risk // 100:,}  ({got / risk:.1%})")
    for name in rr.baselines:
        b = rr.baseline_summary(name)
        print(f"  baseline {name:<16}: Rs {b['recovered_paise'] // 100:,} "
              f"({b['recovered_paise'] / risk:.1%})")
    print(f"  guardrail violations : {guardrails.count_violations(rr.results)}")
    print(f"  audit                : {'VERIFIED' if rr.audit_ok else 'FAILED'} "
          f"({rr.audit_manifest.get('record_count')} records)")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Run the whole batch and write the report.")
    parser.add_argument("--seed", type=int, default=config.RANDOM_SEED)
    parser.add_argument("--count", type=int, default=config.BATCH_SIZE)
    parser.add_argument("--out", type=Path, default=config.REPORT_FILE)
    parser.add_argument("--dump", type=Path, default=None)
    args = parser.parse_args(argv)

    rr = run(args.seed, args.count)
    _print_headline(rr)

    if args.dump:
        import json
        with Path(args.dump).open("w", encoding="utf-8") as fh:
            for r in rr.results:
                fh.write(json.dumps(result_to_dict(r), ensure_ascii=False) + "\n")

    report.write(rr, args.out)
    print(f"\n  report               : {args.out}\n")


if __name__ == "__main__":
    main()
