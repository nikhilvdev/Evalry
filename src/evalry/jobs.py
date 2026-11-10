"""Job model and in-process queue (plan §7). One job per task x sample x stage, keyed by its inputs.

Idempotent by construction: submitting a job whose key already exists returns the existing job, and a finished job is never
executed or charged twice. shortcut: in-memory and single process; a Postgres-backed store replaces ``JobQueue`` behind the
same methods when the platform needs durability or multiple workers.
"""

import hashlib
import json
import threading
from enum import IntEnum, StrEnum
from itertools import count

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
    project_id: str, suite_version: str, task_hash: str, sample: int, stage: Stage, generator_hash: str
) -> str:
    """Same inputs, same key. Anything that can change the result belongs here."""
    parts = [project_id, suite_version, task_hash, sample, stage.value, generator_hash]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    project_id: str
    task_id: str
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


class JobQueue:
    def __init__(self, limits: dict[str, ProjectLimits] | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: dict[str, int] = {}  # submission order, so equal priorities run FIFO
        self._seq = count()
        self._limits = limits or {}
        self._spent: dict[str, float] = {}
        self._lock = threading.Lock()

    def _limit(self, project_id: str) -> ProjectLimits:
        return self._limits.get(project_id, ProjectLimits())

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
            running: dict[str, int] = {}
            for j in self._jobs.values():
                if j.state is State.RUNNING:
                    running[j.project_id] = running.get(j.project_id, 0) + 1
            queued = sorted(
                (j for j in self._jobs.values() if j.state is State.QUEUED),
                key=lambda j: (j.priority, self._order[j.key]),
            )
            for j in queued:
                lim = self._limit(j.project_id)
                if running.get(j.project_id, 0) >= lim.max_concurrent:
                    continue
                if self._spent.get(j.project_id, 0.0) >= lim.budget_usd:
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
            self._spent[j.project_id] = self._spent.get(j.project_id, 0.0) + result.cost_usd

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

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, key: str) -> Job:
        with self._lock:
            return self._jobs[key]

    def spent(self, project_id: str) -> float:
        with self._lock:
            return self._spent.get(project_id, 0.0)

    def blocked_by_budget(self, project_id: str) -> list[Job]:
        """Queued jobs of a project that is at its budget: they will never run, so report them rather than wait."""
        with self._lock:
            if self._spent.get(project_id, 0.0) < self._limit(project_id).budget_usd:
                return []
            return [j for j in self._jobs.values() if j.project_id == project_id and j.state is State.QUEUED]
