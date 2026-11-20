"""Version 1 public schemas: task YAML and run record (plan §18). Changing these needs a new major version."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from evalry.safepath import safe_relpath

SCHEMA_VERSION: Literal["1"] = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepoSpec(_Strict):
    """What a tier 3 or 4 task runs against (plan §4): a pinned snapshot, the image that has its dependencies, how to test it."""

    snapshot: str = Field(
        pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the snapshot tar.gz in object storage."
    )
    commit: str = Field(min_length=7, max_length=64, description="The commit the snapshot was taken from.")
    image: str = Field(
        pattern=r"^.+@sha256:[0-9a-f]{64}$",
        description="Runner image, pinned by digest, with the repo's dependencies baked in.",
    )
    test_command: list[str] = Field(min_length=1, max_length=20)


class AgentSpec(_Strict):
    """Limits for a tier 4 task; reaching one ends the run and is recorded as a failure category."""

    max_steps: int = Field(default=30, ge=1, le=200)
    max_wall_s: int = Field(default=900, ge=10, le=7200)
    max_cost_usd: float = Field(default=2.0, gt=0)


class Task(_Strict):
    """A task: one prompt and hidden tests (tiers 1 and 2), or a pinned repository (tiers 3 and 4) (plan §4).

    Tiers 3 and 4 add ``repo`` (and ``agent`` for tier 4). ``tests`` then holds the hidden test files as a JSON object of
    relative path to content, copied over the model's edits before the repo's own test command runs.
    """

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
    repo: RepoSpec | None = None
    agent: AgentSpec | None = None

    @model_serializer(mode="wrap")
    def _omit_unused_extensions(self, handler: Any) -> dict[str, Any]:
        """Tier 1 and 2 tasks serialise exactly as before the extensions existed, so their hashes and stored JSON do not move."""
        data: dict[str, Any] = handler(self)
        for key in ("repo", "agent"):
            if data.get(key) is None:
                data.pop(key, None)
        return data

    @model_validator(mode="after")
    def _tier_shape(self) -> "Task":
        if self.tier in (1, 2):
            if self.repo or self.agent:
                raise ValueError(
                    f"Tier {self.tier} tasks do not take repo or agent; those are for tiers 3 and 4."
                )
            return self
        if self.repo is None:
            raise ValueError(f"Tier {self.tier} tasks need a repo (snapshot, commit, image, test_command).")
        if (self.tier == 4) != (self.agent is not None):
            raise ValueError("A tier 4 task needs agent limits, and only tier 4 tasks take them.")
        try:
            files = json.loads(self.tests)
            if not isinstance(files, dict) or len(files) > 200:
                raise TypeError("not an object of at most 200 files")
            for path, content in files.items():
                safe_relpath(path)
                if not isinstance(content, str):
                    raise TypeError(f"content of {path!r} is not a string")
        except (ValueError, TypeError) as e:  # includes json.JSONDecodeError
            raise ValueError(
                f"For tier {self.tier}, tests must be a JSON object of relative path to hidden test file content: {e}"
            ) from e
        return self


class GraderScore(_Strict):
    grader: str
    grader_version: str
    score: float = Field(ge=0, le=1)
    evidence: str = ""


class AgentMetrics(_Strict):
    """What a tier 4 run did (plan §4 "Agent metrics"). ``failure_categories`` explains how a run went wrong."""

    steps: int = Field(ge=0)
    tool_calls: dict[str, int]
    tokens_in: int = Field(ge=0)
    tokens_out: int = Field(ge=0)
    wall_s: float = Field(ge=0)
    finished: bool = Field(description="The agent ended on its own rather than at a limit or a failure.")
    failure_categories: list[str] = []


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
    code_digest: str | None = Field(
        default=None,
        description="SHA-256 of the code in object storage. Set when a run is stored with an object store, in which "
        "case the stored record has an empty `code`; reads put the code back.",
    )
    dataset_version: str | None = None
    judge_version: str | None = None
    agent: AgentMetrics | None = None

    @model_serializer(mode="wrap")
    def _omit_agent_when_absent(self, handler: Any) -> dict[str, Any]:
        """Records of tier 1 to 3 samples serialise exactly as before agent metrics existed."""
        data: dict[str, Any] = handler(self)
        if data.get("agent") is None:
            data.pop("agent", None)
        return data


class RunRecord(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    run_id: str
    suite_version: str
    started_at: datetime
    results: list[SampleResult]


def load_task(path: Path) -> Task:
    return Task.model_validate(yaml.safe_load(path.read_text()))
