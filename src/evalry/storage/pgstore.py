"""PostgreSQL implementation of ``Backend`` (plan §8).

Every call runs in one transaction that first binds ``evalry.tenant_id`` (row-level security) and every query also filters by
tenant_id explicitly, so a mistake in either layer alone does not cross tenants. The application connects as a least-privilege
role; creating tenants uses a separate admin connection because it is an operator action.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from evalry.audit.chain import GENESIS, AuditEvent
from evalry.disputes import Dispute, Resolution
from evalry.gates.hard import GateDecision
from evalry.judge.calibration import CalibrationRecord
from evalry.labeling import Label, LabelBatch
from evalry.monitoring import Closure, Incident, Signal
from evalry.schemas import RunRecord, Task
from evalry.storage.backend import suite_hash


class UnsafeConnection(Exception):
    """The application role would bypass row-level security. Connect as a plain role, see 0001_init.sql."""


class PostgresBackend:
    def __init__(self, app_dsn: str, *, admin_dsn: str | None = None, max_connections: int = 10) -> None:
        self._admin_dsn = admin_dsn
        self._pool = ConnectionPool(app_dsn, min_size=1, max_size=max_connections, open=True)
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "SELECT rolsuper OR rolbypassrls, current_user FROM pg_roles WHERE rolname = current_user"
                ).fetchone()
            if row is None or row[0]:
                raise UnsafeConnection(
                    f"Database role {row[1] if row else '?'!r} is a superuser or has BYPASSRLS, so tenant isolation in the "
                    "database would not apply. Connect the application as an ordinary role (evalry_app)."
                )
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _tx(self, tenant_id: str) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT set_config('evalry.tenant_id', %s, true)", (tenant_id,)
            )  # true: this transaction only
            yield conn

    # -- tenants (operator) ----------------------------------------------------------------------------------------

    def create_tenant(self, tenant_id: str, admin_subject: str) -> bool:
        if self._admin_dsn is None:
            raise RuntimeError(
                "Creating a tenant needs an admin connection. Pass admin_dsn to PostgresBackend."
            )
        with psycopg.connect(self._admin_dsn) as conn, conn.transaction():
            conn.execute("SELECT set_config('evalry.tenant_id', %s, true)", (tenant_id,))
            if (
                conn.execute(
                    "INSERT INTO tenants (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING", (tenant_id,)
                ).rowcount
                == 0
            ):
                return False
            conn.execute(
                "INSERT INTO tenant_admins (tenant_id, subject) VALUES (%s, %s)", (tenant_id, admin_subject)
            )
            return True

    def is_tenant_admin(self, tenant_id: str, subject: str) -> bool:
        with self._tx(tenant_id) as c:
            return (
                c.execute(
                    "SELECT 1 FROM tenant_admins WHERE tenant_id = %s AND subject = %s", (tenant_id, subject)
                ).fetchone()
                is not None
            )

    # -- projects and members --------------------------------------------------------------------------------------

    def project_exists(self, tenant_id: str, project_id: str) -> bool:
        with self._tx(tenant_id) as c:
            return (
                c.execute(
                    "SELECT 1 FROM projects WHERE tenant_id = %s AND project_id = %s", (tenant_id, project_id)
                ).fetchone()
                is not None
            )

    def create_project(self, tenant_id: str, project_id: str) -> bool:
        with self._tx(tenant_id) as c:
            return (
                c.execute(
                    "INSERT INTO projects (tenant_id, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (tenant_id, project_id),
                ).rowcount
                == 1
            )

    def member_role(self, tenant_id: str, project_id: str, subject: str) -> int | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT role FROM project_members WHERE tenant_id = %s AND project_id = %s AND subject = %s",
                (tenant_id, project_id, subject),
            ).fetchone()
        return None if row is None else int(row[0])

    def set_member(self, tenant_id: str, project_id: str, subject: str, role: int) -> None:
        with self._tx(tenant_id) as c:
            c.execute(
                "INSERT INTO project_members (tenant_id, project_id, subject, role) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, project_id, subject) DO UPDATE SET role = EXCLUDED.role",
                (tenant_id, project_id, subject, role),
            )

    def remove_member(self, tenant_id: str, project_id: str, subject: str) -> int | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "DELETE FROM project_members WHERE tenant_id = %s AND project_id = %s AND subject = %s RETURNING role",
                (tenant_id, project_id, subject),
            ).fetchone()
        return None if row is None else int(row[0])

    # -- runs and decisions (immutable) ----------------------------------------------------------------------------

    def put_run(self, tenant_id: str, project_id: str, record: RunRecord) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO runs (tenant_id, project_id, run_id, suite_version, started_at, record) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, record.run_id, record.suite_version, record.started_at,
                 Jsonb(record.model_dump(mode="json"))),
            ).rowcount == 1  # fmt: skip

    def get_run(self, tenant_id: str, project_id: str, run_id: str) -> RunRecord | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT record FROM runs WHERE tenant_id = %s AND project_id = %s AND run_id = %s",
                (tenant_id, project_id, run_id),
            ).fetchone()
        return None if row is None else RunRecord.model_validate(row[0])

    def list_runs(self, tenant_id: str, project_id: str) -> list[str]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                'SELECT run_id FROM runs WHERE tenant_id = %s AND project_id = %s ORDER BY run_id COLLATE "C"',
                (tenant_id, project_id),
            ).fetchall()
        return [r[0] for r in rows]

    def put_decision(self, tenant_id: str, project_id: str, decision: GateDecision) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO gate_decisions (tenant_id, project_id, decision_id, run_id, outcome, environment, decision) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, decision.decision_id, decision.run_id, decision.outcome, decision.environment,
                 Jsonb(decision.model_dump(mode="json"))),
            ).rowcount == 1  # fmt: skip

    def get_decision(self, tenant_id: str, project_id: str, decision_id: str) -> GateDecision | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT decision FROM gate_decisions WHERE tenant_id = %s AND project_id = %s AND decision_id = %s",
                (tenant_id, project_id, decision_id),
            ).fetchone()
        return None if row is None else GateDecision.model_validate(row[0])

    def list_decisions(self, tenant_id: str, project_id: str) -> list[GateDecision]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT decision FROM gate_decisions WHERE tenant_id = %s AND project_id = %s ORDER BY recorded_at, decision_id",
                (tenant_id, project_id),
            ).fetchall()
        return [GateDecision.model_validate(r[0]) for r in rows]

    # -- judge calibrations (append-only) -----------------------------------------------------------------------------

    def put_calibration(self, tenant_id: str, project_id: str, record: CalibrationRecord) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO judge_calibrations (tenant_id, project_id, calibration_id, judge_version, passed, record, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, record.calibration_id, record.judge_version, record.passed,
                 Jsonb(record.model_dump(mode="json")), record.created_at),
            ).rowcount == 1  # fmt: skip

    def list_calibrations(self, tenant_id: str, project_id: str) -> list[CalibrationRecord]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT record FROM judge_calibrations WHERE tenant_id = %s AND project_id = %s ORDER BY created_at, calibration_id",
                (tenant_id, project_id),
            ).fetchall()
        return [CalibrationRecord.model_validate(r[0]) for r in rows]

    # -- disputes (append-only) -----------------------------------------------------------------------------------------

    def put_dispute(self, tenant_id: str, project_id: str, dispute: Dispute) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO disputes (tenant_id, project_id, decision_id, dispute, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, dispute.decision_id, Jsonb(dispute.model_dump(mode="json")), dispute.created_at),
            ).rowcount == 1  # fmt: skip

    def get_dispute(self, tenant_id: str, project_id: str, decision_id: str) -> Dispute | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT dispute FROM disputes WHERE tenant_id = %s AND project_id = %s AND decision_id = %s",
                (tenant_id, project_id, decision_id),
            ).fetchone()
        return None if row is None else Dispute.model_validate(row[0])

    def list_disputes(self, tenant_id: str, project_id: str) -> list[Dispute]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT dispute FROM disputes WHERE tenant_id = %s AND project_id = %s ORDER BY created_at, decision_id",
                (tenant_id, project_id),
            ).fetchall()
        return [Dispute.model_validate(r[0]) for r in rows]

    def put_resolution(self, tenant_id: str, project_id: str, resolution: Resolution) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO dispute_resolutions (tenant_id, project_id, decision_id, resolution, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, resolution.decision_id, Jsonb(resolution.model_dump(mode="json")), resolution.created_at),
            ).rowcount == 1  # fmt: skip

    def get_resolution(self, tenant_id: str, project_id: str, decision_id: str) -> Resolution | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT resolution FROM dispute_resolutions WHERE tenant_id = %s AND project_id = %s AND decision_id = %s",
                (tenant_id, project_id, decision_id),
            ).fetchone()
        return None if row is None else Resolution.model_validate(row[0])

    # -- production monitoring (append-only) ----------------------------------------------------------------------------

    def put_signals(self, tenant_id: str, project_id: str, signals: list[Signal]) -> int:
        new = 0
        with self._tx(tenant_id) as c:
            for s in signals:
                new += c.execute(
                    "INSERT INTO production_signals (tenant_id, project_id, signal_id, task_id, kind, observed_at, signal) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        tenant_id,
                        project_id,
                        s.signal_id,
                        s.task_id,
                        s.kind,
                        s.observed_at,
                        Jsonb(s.model_dump(mode="json")),
                    ),
                ).rowcount
        return new

    def list_signals(self, tenant_id: str, project_id: str, since: datetime) -> list[Signal]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT signal FROM production_signals WHERE tenant_id = %s AND project_id = %s AND observed_at >= %s",
                (tenant_id, project_id, since),
            ).fetchall()
        return [Signal.model_validate(r[0]) for r in rows]

    def put_incident(self, tenant_id: str, project_id: str, incident: Incident) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO eval_incidents (tenant_id, project_id, incident_id, task_id, incident, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, incident.incident_id, incident.task_id, Jsonb(incident.model_dump(mode="json")), incident.created_at),
            ).rowcount == 1  # fmt: skip

    def list_incidents(self, tenant_id: str, project_id: str) -> list[Incident]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT incident FROM eval_incidents WHERE tenant_id = %s AND project_id = %s ORDER BY created_at, incident_id",
                (tenant_id, project_id),
            ).fetchall()
        return [Incident.model_validate(r[0]) for r in rows]

    def put_closure(self, tenant_id: str, project_id: str, closure: Closure) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO incident_closures (tenant_id, project_id, incident_id, closure, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, closure.incident_id, Jsonb(closure.model_dump(mode="json")), closure.created_at),
            ).rowcount == 1  # fmt: skip

    def list_closures(self, tenant_id: str, project_id: str) -> list[Closure]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT closure FROM incident_closures WHERE tenant_id = %s AND project_id = %s",
                (tenant_id, project_id),
            ).fetchall()
        return [Closure.model_validate(r[0]) for r in rows]

    # -- human labeling (append-only) ---------------------------------------------------------------------------------

    def put_label_batch(self, tenant_id: str, project_id: str, batch: LabelBatch) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO label_batches (tenant_id, project_id, batch_id, batch, created_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, batch.batch_id, Jsonb(batch.model_dump(mode="json")), batch.created_at),
            ).rowcount == 1  # fmt: skip

    def get_label_batch(self, tenant_id: str, project_id: str, batch_id: str) -> LabelBatch | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT batch FROM label_batches WHERE tenant_id = %s AND project_id = %s AND batch_id = %s",
                (tenant_id, project_id, batch_id),
            ).fetchone()
        return None if row is None else LabelBatch.model_validate(row[0])

    def put_label(self, tenant_id: str, project_id: str, label: Label) -> bool:
        with self._tx(tenant_id) as c:
            return c.execute(
                "INSERT INTO labels (tenant_id, project_id, batch_id, item_id, reviewer, kind, label, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (tenant_id, project_id, label.batch_id, label.item_id, label.reviewer, label.kind,
                 Jsonb(label.model_dump(mode="json")), label.created_at),
            ).rowcount == 1  # fmt: skip

    def list_labels(self, tenant_id: str, project_id: str, batch_id: str) -> list[Label]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                "SELECT label FROM labels WHERE tenant_id = %s AND project_id = %s AND batch_id = %s ORDER BY created_at, item_id",
                (tenant_id, project_id, batch_id),
            ).fetchall()
        return [Label.model_validate(r[0]) for r in rows]

    # -- suites (immutable versions) --------------------------------------------------------------------------------

    def put_suite(
        self, tenant_id: str, project_id: str, version: str, tasks: list[Task], created_by: str
    ) -> str | None:
        digest = suite_hash(tasks)
        with self._tx(tenant_id) as c:
            added = c.execute(
                "INSERT INTO suites (tenant_id, project_id, suite_version, content_hash, tasks, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    tenant_id,
                    project_id,
                    version,
                    digest,
                    Jsonb([t.model_dump(mode="json") for t in tasks]),
                    created_by,
                ),
            ).rowcount
        return digest if added == 1 else None

    def get_suite(self, tenant_id: str, project_id: str, version: str) -> list[Task] | None:
        with self._tx(tenant_id) as c:
            row = c.execute(
                "SELECT tasks FROM suites WHERE tenant_id = %s AND project_id = %s AND suite_version = %s",
                (tenant_id, project_id, version),
            ).fetchone()
        return None if row is None else [Task.model_validate(t) for t in row[0]]

    def list_suites(self, tenant_id: str, project_id: str) -> list[str]:
        with self._tx(tenant_id) as c:
            rows = c.execute(
                'SELECT suite_version FROM suites WHERE tenant_id = %s AND project_id = %s ORDER BY suite_version COLLATE "C"',
                (tenant_id, project_id),
            ).fetchall()
        return [r[0] for r in rows]


class PostgresAuditSink:
    """Audit chains in PostgreSQL, one per tenant. Appends take a per-chain advisory lock inside the transaction, so
    concurrent writers (several API processes) extend the chain one after another instead of forking it."""

    def __init__(self, backend: PostgresBackend) -> None:
        self._pool = backend._pool

    def append(self, tenant_id: str | None, make: Callable[[int, str], AuditEvent]) -> AuditEvent:
        if not tenant_id:
            raise ValueError("Audit events in the database belong to a tenant. Pass tenant_id.")
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('evalry.tenant_id', %s, true)", (tenant_id,))
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (tenant_id,))
            last = conn.execute(
                "SELECT seq, hash FROM audit_events WHERE chain_id = %s ORDER BY seq DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            event = make(0, GENESIS) if last is None else make(last[0] + 1, last[1])
            conn.execute(
                "INSERT INTO audit_events (chain_id, seq, ts, actor, action, target, project_id, hash, event) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, event.seq, event.ts, event.actor, event.action, event.target, event.project_id, event.hash,
                 Jsonb(event.model_dump(mode="json"))),
            )  # fmt: skip
            return event

    def events(self, tenant_id: str | None = None) -> list[AuditEvent]:
        if not tenant_id:
            raise ValueError("Audit events in the database belong to a tenant. Pass tenant_id.")
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('evalry.tenant_id', %s, true)", (tenant_id,))
            rows = conn.execute(
                "SELECT event FROM audit_events WHERE chain_id = %s ORDER BY seq", (tenant_id,)
            ).fetchall()
        return [AuditEvent.model_validate(r[0]) for r in rows]
