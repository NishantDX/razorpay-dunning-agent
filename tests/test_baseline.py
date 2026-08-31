"""Tests for the naive baselines."""
import random

from dunning import baseline
from tests.test_execute import _case


def test_naive_recovers_when_first_roll_wins():
    r = baseline.naive_one_retry(_case("bank_timeout", base_recovery_prob=1.0,
                                       transient_retry_prob=1.0), seed=1)
    assert r.recovered and r.attempts == 1 and r.amount_recovered_paise == 200000


def test_naive_gives_up_after_one():
    r = baseline.naive_one_retry(_case("do_not_honour"), seed=1)  # zero latent
    assert not r.recovered and r.attempts == 1 and r.amount_recovered_paise == 0


def test_blind_three_tries_up_to_three():
    r = baseline.blind_three(_case("do_not_honour"), seed=1)
    assert not r.recovered and r.attempts == 3


def test_shares_the_agents_outcome_rng():
    case = _case("bank_timeout", base_recovery_prob=0.5)
    # the baseline's first draw must be the agent's first draw for the same case
    expected = random.Random(f"7:{case['case_id']}:outcome").random()
    seen = {}
    orig = baseline._recovered

    def spy(c, action, clock, rng):
        seen.setdefault("first", rng.random())
        return False

    baseline._recovered = spy
    try:
        baseline.naive_one_retry(case, seed=7)
    finally:
        baseline._recovered = orig
    assert abs(seen["first"] - expected) < 1e-12


def test_deterministic():
    c = _case("insufficient_funds", base_recovery_prob=0.4)
    assert baseline.blind_three(c, seed=3) == baseline.blind_three(c, seed=3)
