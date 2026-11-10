"""Run a suite: every task x N samples, then summarise per task (plan §6)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from evalry.cli import run_sample
from evalry.generation import Generator
from evalry.schemas import RunRecord, SampleResult, Task, load_task
from evalry.scoring import TaskStats, pass_at_k, task_stats


class TaskSummary(BaseModel):
    stats: TaskStats
    pass_at_k: float | None = None  # None when fewer than k samples were drawn


def load_suite(directory: Path) -> list[Task]:
    tasks = [load_task(p) for p in sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])]
    ids = [t.id for t in tasks]
    if not tasks:
        raise ValueError(f"No task YAML files in {directory}. Point --suite at a directory of v1 task files.")
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(
            f"Duplicate task ids in {directory}: {sorted(dupes)}. Task ids must be unique within a suite."
        )
    return tasks


def run_suite(
    tasks: list[Task],
    make_generator: Callable[[Task], Generator],
    *,
    samples: int,
    suite_version: str,
    timeout_s: float = 10,
) -> RunRecord:
    """Sequential on purpose: parallel, resumable, idempotent jobs are the phase 2 scheduler's job."""
    if samples < 1:
        raise ValueError("samples must be at least 1.")
    results: list[SampleResult] = []
    started = datetime.now(UTC)
    for task in tasks:
        gen = make_generator(task)
        results += [run_sample(task, gen, i, timeout_s=timeout_s) for i in range(samples)]
    return RunRecord(
        run_id=uuid.uuid4().hex, suite_version=suite_version, started_at=started, results=results
    )


def summarize(record: RunRecord, k: int = 1) -> dict[str, TaskSummary]:
    """Per-task stats over samples; a sample 'passes' when its composite is 1.0 (all graders full marks)."""
    by_task: dict[str, list[float]] = {}
    for r in record.results:
        by_task.setdefault(r.task_id, []).append(r.composite)
    return {
        tid: TaskSummary(
            stats=task_stats(v),
            pass_at_k=pass_at_k(len(v), sum(x == 1.0 for x in v), k) if len(v) >= k else None,
        )
        for tid, v in by_task.items()
    }
