"""Version 1 public schemas: task YAML and run record (plan §18). Changing these needs a new major version."""

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1"] = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Task(_Strict):
    """A tier-1 task: one prompt, hidden tests, optional reference solution (plan §4)."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    id: str = Field(min_length=1)
    language: str = Field(min_length=1)
    tier: Literal[1, 2, 3, 4] = 1
    prompt: str = Field(min_length=1)
    tests: str = Field(min_length=1, description="Hidden tests; never sent to generators.")
    reference: str | None = None
    tags: list[str] = []
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    owner: str | None = None


class GraderScore(_Strict):
    grader: str
    grader_version: str
    score: float = Field(ge=0, le=1)
    evidence: str = ""


class SampleResult(_Strict):
    """Immutable result of one task x sample; records every version needed to reproduce it (plan §8)."""

    task_id: str
    task_hash: str
    sample: int = Field(ge=0)
    generator_model: str = Field(description="Identifier the provider returned, not the one requested.")
    code: str
    scores: list[GraderScore]
    composite: float = Field(ge=0, le=1)
    cost_usd: float = Field(ge=0, default=0)
    # Reproducibility (plan §8). Optional so records written before these existed stay valid v1 records.
    image: str | None = Field(default=None, description="Runner image digest the grading ran in.")
    seed: int | None = Field(
        default=None, description="Interpreter hash seed; same code + seed + image = same score."
    )
    isolation: Literal["rpc", "inprocess"] | None = Field(
        default=None,
        description="rpc: tests ran in a separate container; inprocess: same process (advisory only).",
    )
    dataset_version: str | None = None
    judge_version: str | None = None


class RunRecord(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    run_id: str
    suite_version: str
    started_at: datetime
    results: list[SampleResult]


def load_task(path: Path) -> Task:
    return Task.model_validate(yaml.safe_load(path.read_text()))
