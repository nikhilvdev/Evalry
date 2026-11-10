"""Run comparison: paired bootstrap over tasks and the regression gate rule (plan §6)."""

import random
from typing import NamedTuple

MIN_TASKS = 100
MIN_SAMPLES = 5


class Delta(NamedTuple):
    mean: float
    lo: float
    hi: float


def paired_bootstrap(
    baseline: dict[str, float],
    candidate: dict[str, float],
    *,
    n_boot: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Delta:
    """CI on mean(candidate - baseline) over tasks present in both runs; seeded so results reproduce."""
    ids = sorted(baseline.keys() & candidate.keys())
    if not ids:
        raise ValueError("Runs share no tasks; compare runs of the same suite version.")
    diffs = [candidate[i] - baseline[i] for i in ids]
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(n_boot))
    return Delta(
        sum(diffs) / len(diffs), means[int(n_boot * alpha / 2)], means[int(n_boot * (1 - alpha / 2)) - 1]
    )


def regression_gate(delta: Delta, threshold: float, *, n_tasks: int, min_samples: int) -> bool:
    """True when the gate blocks: the interval's upper bound is below -threshold.

    Refuses to decide on underpowered runs rather than pass or fail them silently.
    """
    if n_tasks < MIN_TASKS or min_samples < MIN_SAMPLES:
        raise ValueError(
            f"Regression gates need >= {MIN_TASKS} tasks and >= {MIN_SAMPLES} samples/task; got "
            f"{n_tasks} and {min_samples}. Run the full tier or use an advisory gate."
        )
    return delta.hi < -threshold
