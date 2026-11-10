"""Run a suite: every task x N samples, then summarise per task (plan §6)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from logquill import bind_context
from pydantic import BaseModel

from evalry.audit import AuditLog
from evalry.cache import GradeCache
from evalry.generation import Generator
from evalry.graders import DEFAULT_ISOLATION, Isolation
from evalry.jobs import JobQueue, ProjectLimits
from evalry.schemas import RunRecord, Task, load_task
from evalry.scoring import TaskStats, pass_at_k, task_stats
from evalry.worker import collect, sandbox_executor, submit_run, work


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
    make_generator: Callable[[Task, int], Generator],
    *,
    samples: int,
    suite_version: str,
    timeout_s: float = 10,
    cache: GradeCache | None = None,
    isolation: Isolation = DEFAULT_ISOLATION,
    project_id: str = "local",
    generator_hash: str = "local",
    budget_usd: float = float("inf"),
    audit: AuditLog | None = None,
    actor: str = "local",
) -> RunRecord:
    """Run through the job queue so the CLI and the future API share one path: idempotent keys, retries, budget stop, audit.

    shortcut: one in-process worker and an in-memory queue, so a killed process starts over. Durable resume and parallel
    workers come with the persistent store.
    """
    if samples < 1:
        raise ValueError("samples must be at least 1.")
    run_id = uuid.uuid4().hex
    started = datetime.now(UTC)
    queue = JobQueue({project_id: ProjectLimits(budget_usd=budget_usd)})
    # Every log record under this block carries run_id and suite_version (CLAUDE.md logging rules).
    with bind_context(run_id=run_id, suite_version=suite_version):
        jobs = submit_run(
            queue, tasks, project_id=project_id, suite_version=suite_version, generator_hash=generator_hash,
            samples=samples, audit=audit, actor=actor,
        )  # fmt: skip
        work(
            queue,
            {t.id: t for t in tasks},
            sandbox_executor(make_generator, timeout_s=timeout_s, cache=cache, isolation=isolation),
            audit=audit,
        )
        return collect(queue, jobs, run_id=run_id, suite_version=suite_version, started_at=started)


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
