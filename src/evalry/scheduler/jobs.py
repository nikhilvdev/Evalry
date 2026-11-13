"""Job model and in-process queue (plan §7). One job per task x sample x stage, keyed by its inputs.

Idempotent by construction: submitting a job whose key already exists returns the existing job, and a finished job is never
executed or charged twice. This in-memory queue is single process; ``evalry.storage.pgjobs.PostgresJobQueue`` implements the
same ``JobStore`` methods durably and across workers.
"""

import hashlib
import json
import threading
from enum import IntEnum, StrEnum
from itertools import count
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from evalry.schemas import SampleResult


class Priority(IntEnum):
    """Lower runs first: pull-request smoke runs ahead of nightly suites."""

    SMOKE = 0
    NIGHTLY = 1


class Stage(StrEnum):
    GENERATE = "generate"
    GRADE = "grade"
    JUDGE = "judge"


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def job_key(
    project_id: str,
    suite_version: str,
    task_hash: str,
    sample: int,
    stage: Stage,
    generator_hash: str,
    tenant_id: str = "local",
) -> str:
    """Same inputs, same key. Anything that can change the result belongs here, including the tenant."""
    parts = [tenant_id, project_id, suite_version, task_hash, sample, stage.value, generator_hash]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    tenant_id: str = "local"
    project_id: str
    task_id: str
    run_id: str = ""  # which run assembles this job's result; empty for local runs
    suite_version: str = ""  # where a worker finds the task, since the job row holds only its id
    sample: int = Field(ge=0)
    stage: Stage = Stage.GRADE
    priority: Priority = Priority.NIGHTLY
    state: State = State.QUEUED
    attempts: int = 0
    max_attempts: int = Field(default=3, ge=1)
    error: str = ""
    result: SampleResult | None = None


class ProjectLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent: int = Field(default=4, ge=1)
    budget_usd: float = Field(default=float("inf"), ge=0)


class JobStore(Protocol):
    """What the worker needs from a queue: ``JobQueue`` (in-process) or ``PostgresJobQueue`` (durable, many workers)."""

    def submit(self, job: Job) -> Job: ...
    def claim(self) -> Job | None: ...
    def complete(self, key: str, result: SampleResult) -> None: ...
    def fail(self, key: str, error: str) -> None: ...
    def recover(self) -> int: ...
    def running(self) -> int: ...
    def jobs(self) -> list[Job]: ...
    def get(self, key: str) -> Job: ...
    def run_jobs(self, run_id: str) -> list[Job]: ...
    def spent(self, project_id: str, tenant_id: str = "local") -> float: ...
    def blocked_by_budget(self, project_id: str, tenant_id: str = "local") -> list[Job]: ...


class JobQueue:
    def __init__(self, limits: dict[tuple[str, str], ProjectLimits] | None = None) -> None:
        """``limits`` is keyed by (tenant_id, project_id); a scope without an entry gets the defaults."""
        self._jobs: dict[str, Job] = {}
        self._order: dict[str, int] = {}  # submission order, so equal priorities run FIFO
        self._seq = count()
        self._limits = limits or {}
        self._spent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _scope(job: Job) -> tuple[str, str]:
        return (job.tenant_id, job.project_id)

    def _limit(self, scope: tuple[str, str]) -> ProjectLimits:
        return self._limits.get(scope, ProjectLimits())

    def submit(self, job: Job) -> Job:
        """Idempotent: an existing key returns the stored job untouched, whatever its state."""
        with self._lock:
            if existing := self._jobs.get(job.key):
                return existing
            self._jobs[job.key] = job
            self._order[job.key] = next(self._seq)
            return job

    def claim(self) -> Job | None:
        """Next runnable job: best priority first, FIFO within it, skipping projects at their concurrency or budget limit."""
        with self._lock:
            running: dict[tuple[str, str], int] = {}
            for j in self._jobs.values():
                if j.state is State.RUNNING:
                    running[self._scope(j)] = running.get(self._scope(j), 0) + 1
            queued = sorted(
                (j for j in self._jobs.values() if j.state is State.QUEUED),
                key=lambda j: (j.priority, self._order[j.key]),
            )
            for j in queued:
                scope = self._scope(j)
                lim = self._limit(scope)
                if running.get(scope, 0) >= lim.max_concurrent:
                    continue
                if self._spent.get(scope, 0.0) >= lim.budget_usd:
                    continue
                j.state = State.RUNNING
                j.attempts += 1
                return j
            return None

    def complete(self, key: str, result: SampleResult) -> None:
        with self._lock:
            j = self._jobs[key]
            if j.state is State.DONE:  # a duplicate completion must not charge twice
                return
            j.state, j.result, j.error = State.DONE, result, ""
            self._spent[self._scope(j)] = self._spent.get(self._scope(j), 0.0) + result.cost_usd

    def fail(self, key: str, error: str) -> None:
        """Requeue until ``max_attempts`` is used up, then mark failed so the run can report it."""
        with self._lock:
            j = self._jobs[key]
            j.error = error
            j.state = State.QUEUED if j.attempts < j.max_attempts else State.FAILED

    def recover(self) -> int:
        """Startup recovery: jobs left RUNNING by a crashed worker go back to the queue. Returns how many.

        A job that already used its attempts is failed instead, so one that kills every worker cannot loop forever.
        Call only when no other worker is live; multiple workers need leases with heartbeats (phase 2, durable store).
        """
        with self._lock:
            stuck = [j for j in self._jobs.values() if j.state is State.RUNNING]
            for j in stuck:
                j.state = State.QUEUED if j.attempts < j.max_attempts else State.FAILED
                j.error = j.error or "worker died while running this job"
            return len(stuck)

    def running(self) -> int:
        with self._lock:
            return sum(j.state is State.RUNNING for j in self._jobs.values())

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, key: str) -> Job:
        with self._lock:
            return self._jobs[key]

    def run_jobs(self, run_id: str) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.run_id == run_id]

    def spent(self, project_id: str, tenant_id: str = "local") -> float:
        with self._lock:
            return self._spent.get((tenant_id, project_id), 0.0)

    def blocked_by_budget(self, project_id: str, tenant_id: str = "local") -> list[Job]:
        """Queued jobs of a project that is at its budget: they will never run, so report them rather than wait."""
        with self._lock:
            scope = (tenant_id, project_id)
            if self._spent.get(scope, 0.0) < self._limit(scope).budget_usd:
                return []
            return [j for j in self._jobs.values() if self._scope(j) == scope and j.state is State.QUEUED]
