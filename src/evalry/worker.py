"""Worker loop and run submission on top of the job queue (plan §7)."""

import hashlib
from collections.abc import Callable
from datetime import datetime

from evalry.audit import AuditLog
from evalry.cache import GradeCache
from evalry.engine import run_sample
from evalry.generation import Generator
from evalry.graders import DEFAULT_ISOLATION, Isolation
from evalry.jobs import Job, JobQueue, Priority, Stage, State, job_key
from evalry.log import get_logger
from evalry.schemas import RunRecord, SampleResult, Task

log = get_logger("worker")

Executor = Callable[[Job, Task], SampleResult]


def task_hash(task: Task) -> str:
    return hashlib.sha256(task.model_dump_json().encode()).hexdigest()


def submit_run(
    queue: JobQueue,
    tasks: list[Task],
    *,
    project_id: str,
    suite_version: str,
    generator_hash: str,
    samples: int,
    priority: Priority = Priority.NIGHTLY,
    audit: AuditLog | None = None,
    actor: str = "unknown",
) -> list[Job]:
    """Enqueue task x sample jobs. Safe to call again for the same run: existing keys are returned, not duplicated."""
    if audit:
        audit.append(
            actor,
            "run.submitted",
            f"{project_id}/{suite_version}",
            source="worker",
            project_id=project_id,
            details={"tasks": str(len(tasks)), "samples": str(samples), "priority": priority.name},
        )
    return [
        queue.submit(
            Job(
                key=job_key(project_id, suite_version, task_hash(t), i, Stage.GRADE, generator_hash),
                project_id=project_id,
                task_id=t.id,
                sample=i,
                priority=priority,
            )
        )
        for t in tasks
        for i in range(samples)
    ]


def sandbox_executor(
    make_generator: Callable[[Task, int], Generator],
    *,
    timeout_s: float = 10,
    cache: GradeCache | None = None,
    isolation: Isolation = DEFAULT_ISOLATION,
) -> Executor:
    """Executor that generates then grades in the sandbox. The generator is built per job, so job order cannot matter."""

    def execute(job: Job, task: Task) -> SampleResult:
        return run_sample(
            task,
            make_generator(task, job.sample),
            job.sample,
            timeout_s=timeout_s,
            cache=cache,
            isolation=isolation,
        )

    return execute


def _audit(audit: AuditLog | None, job: Job, action: str, details: dict[str, str]) -> None:
    if audit:
        audit.append(
            "worker",
            action,
            job.key,
            source="worker",
            project_id=job.project_id,
            details={"task_id": job.task_id, **details},
        )


def work(
    queue: JobQueue,
    tasks: dict[str, Task],
    execute: Executor,
    *,
    max_jobs: int | None = None,
    audit: AuditLog | None = None,
) -> int:
    """Claim and run jobs until none are runnable (or ``max_jobs``). Returns jobs executed.

    Only ``Exception`` is caught: a job that raises is retried or failed by the queue. A hard stop (KeyboardInterrupt,
    process kill) leaves the job RUNNING on purpose; ``JobQueue.recover`` requeues it on the next start.
    """
    done = 0
    while max_jobs is None or done < max_jobs:
        job = queue.claim()
        if job is None:
            break
        try:
            result = execute(job, tasks[job.task_id])
        except Exception as e:  # noqa: BLE001 (any job error must be recorded, not crash the worker)
            log.error(
                "job failed",
                job_id=job.key,
                task_id=job.task_id,
                attempt=job.attempts,
                error=type(e).__name__,
            )
            queue.fail(job.key, f"{type(e).__name__}: {e}")
            _audit(audit, job, "job.failed", {"attempt": str(job.attempts), "error": type(e).__name__})
        else:
            queue.complete(job.key, result)
            _audit(
                audit,
                job,
                "job.completed",
                {"cost_usd": f"{result.cost_usd:.6f}", "composite": f"{result.composite:.4f}"},
            )
        done += 1
    return done


def collect(
    queue: JobQueue, jobs: list[Job], *, run_id: str, suite_version: str, started_at: datetime
) -> RunRecord:
    """Assemble the run record from finished jobs. Failed or unfinished jobs are an error, never silently dropped."""
    pending = [j for j in jobs if j.state is not State.DONE]
    if pending:
        detail = ", ".join(f"{j.task_id}#{j.sample}={j.state.value}" for j in pending[:5])
        if pending[0].error:
            detail += f"; first error: {pending[0].error}"
        raise ValueError(
            f"{len(pending)} of {len(jobs)} jobs are not done ({detail}). Resume the run or inspect the errors."
        )
    results = [j.result for j in jobs if j.result is not None]
    return RunRecord(run_id=run_id, suite_version=suite_version, started_at=started_at, results=results)
