"""Composite and per-task scoring rules (plan §6, "Scoring and statistical methods")."""

from math import comb
from statistics import fmean, stdev

from pydantic import BaseModel

from evalry.schemas import GraderScore


class TaskStats(BaseModel):
    mean: float
    min: float
    max: float
    std: float


def composite(
    scores: list[GraderScore],
    weights: dict[str, float],
    *,
    hard_fail: bool = False,
    judge: str = "judge",
    tests: str = "tests",
) -> float:
    """Weighted mean over the graders that ran; weights renormalise over skipped graders.

    ``hard_fail`` is set by the caller for a parse failure, timeout or security finding at/above the
    configured severity: those score 0 before any weighting. The judge is capped at the test pass
    fraction when fewer than half of the tests pass, so a persuasive judge cannot rescue broken code.
    """
    if hard_fail or not scores:
        return 0.0
    by_name = {s.grader: s.score for s in scores}
    if judge in by_name and tests in by_name and by_name[tests] < 0.5:
        by_name[judge] = min(by_name[judge], by_name[tests])
    total = sum(weights.get(g, 1.0) for g in by_name)
    if total <= 0:
        raise ValueError("Grader weights sum to zero; give at least one grader a positive weight.")
    return sum(weights.get(g, 1.0) * v for g, v in by_name.items()) / total


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k estimator: 1 - C(n-c, k) / C(n, k). Needs n >= k samples."""
    if not 0 <= c <= n or k < 1 or n < k:
        raise ValueError(f"pass@k needs 0 <= c <= n and n >= k >= 1; got n={n}, c={c}, k={k}.")
    return 1.0 - comb(n - c, k) / comb(n, k)


def task_stats(values: list[float]) -> TaskStats:
    if not values:
        raise ValueError("task_stats needs at least one sample score.")
    return TaskStats(
        mean=fmean(values), min=min(values), max=max(values), std=stdev(values) if len(values) > 1 else 0.0
    )
