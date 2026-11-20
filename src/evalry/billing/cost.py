"""Pre-run cost estimate (plan §15): Cost = T * N * (C_gen + p_judge * C_judge + C_compute) * (1 - h).

Rates are inputs, never defaults: real prices depend on the providers and cloud chosen (open decision).
"""

from pydantic import BaseModel, ConfigDict, Field


class Rates(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    usd_per_mtok_in: float = Field(ge=0, description="Generator input price per million tokens.")
    usd_per_mtok_out: float = Field(ge=0)
    judge_usd_per_sample: float = Field(ge=0, default=0, description="x2 if each item is judged twice.")
    compute_usd_per_sample: float = Field(ge=0, default=0, description="Container seconds x compute price.")


def estimate(
    rates: Rates,
    *,
    tasks: int,
    samples: int,
    tokens_in: int,
    tokens_out: int,
    p_judge: float = 0,
    cache_hit: float = 0,
) -> float:
    """Expected USD for a run. Show it before the run starts and compare it with the budget."""
    if not (0 <= p_judge <= 1 and 0 <= cache_hit <= 1) or tasks < 0 or samples < 0:
        raise ValueError("p_judge and cache_hit must be in [0, 1]; tasks and samples must be >= 0.")
    c_gen = (tokens_in * rates.usd_per_mtok_in + tokens_out * rates.usd_per_mtok_out) / 1_000_000
    return (
        tasks
        * samples
        * (c_gen + p_judge * rates.judge_usd_per_sample + rates.compute_usd_per_sample)
        * (1 - cache_hit)
    )


def check_budget(cost: float, budget_usd: float) -> None:
    """Hard stop: refuse to start a run whose estimate exceeds the budget."""
    if cost > budget_usd:
        raise ValueError(
            f"Estimated cost ${cost:.2f} exceeds the budget ${budget_usd:.2f}. Lower tasks or samples, or raise the budget."
        )
