import pytest

from evalry.cost import Rates, check_budget, estimate

R = Rates(usd_per_mtok_in=3, usd_per_mtok_out=15, judge_usd_per_sample=0.01, compute_usd_per_sample=0.002)


def test_estimate_matches_formula() -> None:
    # c_gen = (1000*3 + 500*15)/1e6 = 0.0105; per sample = 0.0105 + 0.5*0.01 + 0.002 = 0.0175
    assert estimate(
        R, tasks=100, samples=5, tokens_in=1000, tokens_out=500, p_judge=0.5, cache_hit=0.2
    ) == pytest.approx(100 * 5 * 0.0175 * 0.8)


def test_full_cache_hit_is_free_and_bad_inputs_rejected() -> None:
    assert estimate(R, tasks=10, samples=1, tokens_in=1, tokens_out=1, cache_hit=1) == 0
    with pytest.raises(ValueError):
        estimate(R, tasks=1, samples=1, tokens_in=1, tokens_out=1, p_judge=1.5)


def test_budget_stop() -> None:
    check_budget(1.0, 1.0)
    with pytest.raises(ValueError, match="exceeds the budget"):
        check_budget(1.01, 1.0)
