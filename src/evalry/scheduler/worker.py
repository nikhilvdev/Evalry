"""Worker loop and run submission on top of the job queue (plan §7)."""

import contextvars
import hashlib
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from evalry.audit.chain import AuditLog
from evalry.generation.base import Generator
from evalry.graders.cache import GradeCache
from evalry.graders.python_tests import DEFAULT_ISOLATION, Isolation
from evalry.log import get_logger
from evalry.scheduler.engine import run_sample
from evalry.scheduler.jobs import Job, JobStore, Priority, Stage, State, job_key
from evalry.schemas import RunRecord, SampleResult, Task

log = get_logger("worker")

Executor = Callable[[Job, Task], SampleResult]


def task_hash(task: Task) -> str:
    return hashlib.sha256(task.model_dump_json().encode()).hexdigest()


def submit_run(
    queue: JobStore,
    tasks: list[Task],
    *,
    project_id: str,
    suite_version: str,
    generator_hash: str,
    samples: int,
    priority: Priority = Priority.NIGHTLY,
    audit: AuditLog | None = None,
    actor: str = "unknown",
    tenant_id: str = "local",
) -> list[Job]:
    """Enqueue task x sample jobs. Safe to call again for the same run: existing keys are returned, not duplicated."""
    if audit:
        audit.append(
            actor,
            "run.submitted",
            f"{project_id}/{suite_version}",
            source="worker",
            tenant_id=tenant_id if tenant_id != "local" else None,
            project_id=project_id,
            details={"tasks": str(len(tasks)), "samples": str(samples), "priority": priority.name},
        )
    return [
        queue.submit(
            Job(
                key=job_key(
                    project_id, suite_version, task_hash(t), i, Stage.GRADE, generator_hash, tenant_id
                ),
                tenant_id=tenant_id,
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
            tenant_id=job.tenant_id if job.tenant_id != "local" else None,
            project_id=job.project_id,
            details={"task_id": job.task_id, **details},
        )


class _Budget:
    """Shared ``max_jobs`` allowance across worker threads."""

    def __init__(self, total: int | None) -> None:
        self._left = total
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self._left is None:
                return True
            if self._left <= 0:
                return False
            self._left -= 1
            return True

    def give_back(self) -> None:
        with self._lock:
            if self._left is not None:
                self._left += 1


def _loop(
    queue: JobStore, tasks: dict[str, Task], execute: Executor, audit: AuditLog | None, limit: _Budget
) -> int:
    done = 0
    while limit.take():
        job = queue.claim()
        if job is None:
            limit.give_back()
            if queue.running() == 0:
                break  # nothing runnable and nothing in flight that could requeue: the run is as done as it gets
            time.sleep(0.02)
            continue
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


def work(
    queue: JobStore,
    tasks: dict[str, Task],
    execute: Executor,
    *,
    max_jobs: int | None = None,
    audit: AuditLog | None = None,
    workers: int = 1,
) -> int:
    """Claim and run jobs until none are runnable (or ``max_jobs``). Returns jobs executed.

    ``workers`` threads share the queue; the queue's per-project concurrency limit still caps how many run at once, so more
    workers than the limit just wait. Threads suffice because each job spends its time waiting on a Docker subprocess.

    Only ``Exception`` is caught: a job that raises is retried or failed by the queue. A hard stop (KeyboardInterrupt,
    process kill) leaves the job RUNNING on purpose; ``recover`` requeues it on the next start.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1.")
    limit = _Budget(max_jobs)
    if workers == 1:
        return _loop(queue, tasks, execute, audit, limit)
    # Threads do not inherit contextvars, so each gets its own copy: log records keep run_id and suite_version.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evalry-worker") as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _loop, queue, tasks, execute, audit, limit)
            for _ in range(workers)
        ]
        return sum(f.result() for f in futures)


def collect(
    queue: JobStore, jobs: list[Job], *, run_id: str, suite_version: str, started_at: datetime
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
