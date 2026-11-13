import pytest

from evalry.schemas import GraderScore
from evalry.scoring.compare import Delta, paired_bootstrap, regression_gate
from evalry.scoring.stats import composite, pass_at_k, task_stats


def gs(name: str, score: float) -> GraderScore:
    return GraderScore(grader=name, grader_version="1", score=score)


def test_composite_renormalises_over_skipped_graders() -> None:
    assert composite([gs("tests", 1.0)], {"tests": 3, "judge": 1}) == 1.0
    assert composite([gs("tests", 1.0), gs("judge", 0.0)], {"tests": 3, "judge": 1}) == 0.75


def test_composite_hard_fail_and_empty_score_zero() -> None:
    assert composite([gs("tests", 1.0)], {}, hard_fail=True) == 0.0
    assert composite([], {}) == 0.0


def test_judge_capped_when_under_half_tests_pass() -> None:
    assert composite([gs("tests", 0.25), gs("judge", 1.0)], {}) == 0.25


def test_zero_weights_rejected() -> None:
    with pytest.raises(ValueError):
        composite([gs("tests", 1.0)], {"tests": 0})


def test_pass_at_k() -> None:
    assert pass_at_k(10, 0, 1) == 0.0
    assert pass_at_k(10, 10, 5) == 1.0
    assert pass_at_k(10, 5, 1) == pytest.approx(0.5)
    assert pass_at_k(5, 1, 5) == 1.0  # every sample drawn, so the one pass is always included
    with pytest.raises(ValueError):
        pass_at_k(3, 1, 5)


def test_task_stats() -> None:
    s = task_stats([0.0, 1.0])
    assert (s.mean, s.min, s.max) == (0.5, 0.0, 1.0)
    assert task_stats([0.4]).std == 0.0
    with pytest.raises(ValueError):
        task_stats([])


def test_bootstrap_is_seeded_and_detects_drop() -> None:
    base = {f"t{i}": 1.0 for i in range(100)}
    cand = {f"t{i}": 0.5 for i in range(100)}
    d = paired_bootstrap(base, cand, n_boot=500)
    assert d == paired_bootstrap(base, cand, n_boot=500)
    assert d.mean == -0.5 and d.hi < -0.05
    assert regression_gate(d, 0.05, n_tasks=100, min_samples=5)


def test_gate_does_not_fire_on_noise_and_refuses_underpowered() -> None:
    assert not regression_gate(Delta(-0.02, -0.08, 0.03), 0.05, n_tasks=100, min_samples=5)
    with pytest.raises(ValueError):
        regression_gate(Delta(-1, -1, -1), 0.05, n_tasks=99, min_samples=5)
    with pytest.raises(ValueError):
        paired_bootstrap({"a": 1.0}, {"b": 1.0})
