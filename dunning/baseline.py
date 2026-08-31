"""Step 11 (seeded here in step 10) - naive baselines to compare the agent against.

The agent is only interesting if it beats the dumb thing. These strategies share
the executor's exact outcome oracle and per-case seed, so a strategy and the
agent roll the *same* random numbers - the only difference is the decision logic.

* ``naive_one_retry``  - one immediate retry, then give up.
* ``blind_three``       - three retries 1 hour apart, then give up. (step 11)

No diagnosis, no messages, no links, no timing, no guardrails beyond the attempt
count. Recovery is money in; anything else is money lost.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from dunning import config
from dunning.execute import Clock, _recovered


@dataclass
class BaselineResult:
    case_id: str
    strategy: str
    recovered: bool
    amount_recovered_paise: int
    attempts: int


def _run(case: dict, strategy: str, offsets_hours, seed: int) -> BaselineResult:
    rng = random.Random(f"{seed}:{case['case_id']}:outcome")   # same stream as the agent
    clock = Clock(datetime.fromisoformat(case["failed_at"]))
    for i, off in enumerate(offsets_hours):
        clock.advance_to(clock.now() + timedelta(hours=off))
        if _recovered(case, "retry_now" if i == 0 else "retry_later", clock, rng):
            return BaselineResult(case["case_id"], strategy, True,
                                  case["amount_paise"], i + 1)
    return BaselineResult(case["case_id"], strategy, False, 0, len(list(offsets_hours)))


def naive_one_retry(case: dict, seed: int = None) -> BaselineResult:
    return _run(case, "naive_one_retry", [0], seed or config.RANDOM_SEED)


def blind_three(case: dict, seed: int = None) -> BaselineResult:
    return _run(case, "blind_three", [0, 1, 1], seed or config.RANDOM_SEED)


STRATEGIES = {"naive_one_retry": naive_one_retry, "blind_three": blind_three}
