"""PostgreSQL job queue (plan §7): the durable, multi-worker replacement for the in-process ``JobQueue``.

Two ways to connect, chosen by ``tenant_id``. With one, the connection is bound to that tenant by row-level security (the API's
view: enqueue and read its own jobs). Without one, the connection must be the ``evalry_worker`` role, which claims across
tenants; the database grants that role this table and nothing else. Claims are serialised by an advisory lock so concurrency and
budget limits hold across processes, and each holds a lease: if a worker dies, the next claim requeues its job (0003_jobs.sql).
shortcut: no heartbeat, so a single job must finish within ``lease_s``; add one before jobs can run longer than that.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evalry.scheduler.jobs import Job, Priority, ProjectLimits, Stage, State
from evalry.schemas import SampleResult
from evalry.storage.pgstore import UnsafeConnection

_CLAIM_LOCK = 7_461_922_002  # arbitrary constant shared by every Evalry worker
_COLS = "tenant_id, key, project_id, task_id, sample, stage, priority, state, attempts, max_attempts, error, result"


def _job(row: tuple[Any, ...]) -> Job:
    t, key, project, task, sample, stage, prio, state, attempts, max_attempts, error, result = row
    return Job(
        key=key, tenant_id=t, project_id=project, task_id=task, sample=sample, stage=Stage(stage),
        priority=Priority(prio), state=State(state), attempts=attempts, max_attempts=max_attempts, error=error,
        result=SampleResult.model_validate(result) if result is not None else None,
    )  # fmt: skip


class PostgresJobQueue:
    def __init__(
        self,
        dsn: str,
        *,
        tenant_id: str | None = None,
        limits: dict[tuple[str, str], ProjectLimits] | None = None,
        lease_s: float = 300,
        max_connections: int = 10,
    ) -> None:
        """``limits`` is keyed by (tenant_id, project_id); a scope without an entry gets the defaults."""
        self._tenant = tenant_id
        self._limits = limits or {}
        self._lease_s = lease_s
        self._pool = ConnectionPool(dsn, min_size=1, max_size=max_connections, open=True)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT rolsuper OR rolbypassrls, current_user FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
            if row is None or row[0]:
                raise UnsafeConnection(
                    f"Database role {row[1] if row else '?'!r} is a superuser or has BYPASSRLS, so tenant isolation would "
                    "not apply. Connect as evalry_app (tenant-bound) or evalry_worker."
                )
            if tenant_id is None and row[1] != "evalry_worker":
                raise UnsafeConnection(
                    f"Claiming across tenants needs the evalry_worker role, not {row[1]!r}. "
                    "Pass tenant_id for a tenant-scoped view."
                )
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn, conn.transaction():
            if self._tenant is not None:
                conn.execute("SELECT set_config('evalry.tenant_id', %s, true)", (self._tenant,))
            yield conn

    def _limit(self, scope: tuple[str, str]) -> ProjectLimits:
        return self._limits.get(scope, ProjectLimits())

    def submit(self, job: Job) -> Job:
        """Idempotent: an existing key returns the stored job untouched, whatever its state."""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO jobs (tenant_id, key, project_id, task_id, sample, stage, priority, max_attempts) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, key) DO NOTHING",
                (job.tenant_id, job.key, job.project_id, job.task_id, job.sample, job.stage.value,
                 int(job.priority), job.max_attempts),
            )  # fmt: skip
            row = conn.execute(
                f"SELECT {_COLS} FROM jobs WHERE tenant_id = %s AND key = %s",
                (job.tenant_id, job.key),
            ).fetchone()
        assert row is not None
        return _job(row)

    def recover(self) -> int:
        """Requeue jobs whose worker's lease ran out; fail those with no attempts left. Safe while other workers are live."""
        with self._tx() as conn:
            return self._expire(conn)

    @staticmethod
    def _expire(conn: psycopg.Connection) -> int:
        return conn.execute(
            "UPDATE jobs SET state = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END, "
            "error = CASE WHEN error = '' THEN 'worker died while running this job' ELSE error END, lease_until = NULL "
            "WHERE state = 'running' AND lease_until < now()"
        ).rowcount

    def claim(self) -> Job | None:
        """Next runnable job: best priority first, FIFO within it, skipping projects at their concurrency or budget limit."""
        with self._tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CLAIM_LOCK,))
            self._expire(conn)
            running = {
                (t, p): n
                for t, p, n in conn.execute(
                    "SELECT tenant_id, project_id, count(*) FROM jobs WHERE state = 'running' GROUP BY 1, 2"
                )
            }
            spent = {
                (t, p): float(c)
                for t, p, c in conn.execute(
                    "SELECT tenant_id, project_id, sum(cost_usd) FROM jobs WHERE state = 'done' GROUP BY 1, 2"
                )
            }
            for row in conn.execute(
                f"SELECT {_COLS} FROM jobs WHERE state = 'queued' ORDER BY priority, seq"
            ).fetchall():
                scope = (row[0], row[2])
                lim = self._limit(scope)
                if running.get(scope, 0) >= lim.max_concurrent or spent.get(scope, 0.0) >= lim.budget_usd:
                    continue
                claimed = conn.execute(
                    f"UPDATE jobs SET state = 'running', attempts = attempts + 1, "
                    "lease_until = now() + make_interval(secs => %s) WHERE tenant_id = %s AND key = %s "
                    f"RETURNING {_COLS}",
                    (self._lease_s, row[0], row[1]),
                ).fetchone()
                assert claimed is not None
                return _job(claimed)
            return None

    def complete(self, key: str, result: SampleResult) -> None:
        """A duplicate completion is a no-op, so a job is never charged twice."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET state = 'done', result = %s, cost_usd = %s, error = '', lease_until = NULL "
                "WHERE key = %s AND state <> 'done'",
                (Jsonb(result.model_dump(mode="json")), result.cost_usd, key),
            )

    def fail(self, key: str, error: str) -> None:
        """Requeue until ``max_attempts`` is used up, then mark failed so the run can report it."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE jobs SET error = %s, lease_until = NULL, "
                "state = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'failed' END "
                "WHERE key = %s AND state = 'running'",
                (error, key),
            )

    def running(self) -> int:
        with self._tx() as conn:
            row = conn.execute("SELECT count(*) FROM jobs WHERE state = 'running'").fetchone()
        return int(row[0]) if row else 0

    def jobs(self) -> list[Job]:
        with self._tx() as conn:
            return [_job(r) for r in conn.execute(f"SELECT {_COLS} FROM jobs ORDER BY seq")]

    def get(self, key: str) -> Job:
        with self._tx() as conn:
            row = conn.execute(f"SELECT {_COLS} FROM jobs WHERE key = %s", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return _job(row)

    def spent(self, project_id: str, tenant_id: str = "local") -> float:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT coalesce(sum(cost_usd), 0) FROM jobs WHERE tenant_id = %s AND project_id = %s AND state = 'done'",
                (tenant_id, project_id),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def blocked_by_budget(self, project_id: str, tenant_id: str = "local") -> list[Job]:
        """Queued jobs of a project that is at its budget: they will never run, so report them rather than wait."""
        if self.spent(project_id, tenant_id) < self._limit((tenant_id, project_id)).budget_usd:
            return []
        return [
            j
            for j in self.jobs()
            if (j.tenant_id, j.project_id) == (tenant_id, project_id) and j.state is State.QUEUED
        ]
