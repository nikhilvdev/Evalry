"""The audit chain in PostgreSQL: per-tenant chains, tamper evidence, immutability, isolation, concurrent writers."""

import threading
from pathlib import Path

import psycopg
import pytest

from evalry.access.store import NotFound, Principal, Store
from evalry.audit.chain import GENESIS, AuditLog
from evalry.storage.pgstore import PostgresAuditSink, PostgresBackend
from tests.conftest import PgDb


@pytest.fixture
def pg(pg_db: PgDb) -> PostgresBackend:
    return PostgresBackend(pg_db.app_dsn, admin_dsn=pg_db.admin_dsn)


@pytest.fixture
def audit(pg: PostgresBackend) -> AuditLog:
    return AuditLog(PostgresAuditSink(pg))


def add(a: AuditLog, tenant: str, n: int = 1, actor: str = "alice") -> None:
    for i in range(n):
        a.append(
            actor,
            "run.recorded",
            f"r{i}",
            source="test",
            tenant_id=tenant,
            project_id="app",
            details={"i": str(i)},
        )


def admin(pg_db: PgDb) -> psycopg.Connection:
    return psycopg.connect(pg_db.admin_dsn, autocommit=True)


def test_each_tenant_has_its_own_chain_that_verifies_alone(audit: AuditLog) -> None:
    add(audit, "acme", 3)
    add(audit, "globex", 2)
    add(audit, "acme", 1)
    acme, globex = audit.events("acme"), audit.events("globex")
    assert [e.seq for e in acme] == [0, 1, 2, 3] and [e.seq for e in globex] == [0, 1]
    assert acme[0].prev_hash == GENESIS == globex[0].prev_hash and acme[0].hash != globex[0].hash
    assert audit.verify(tenant_id="acme") == [] and audit.verify(tenant_id="globex") == []
    assert audit.verify(expected_head=audit.head("acme"), tenant_id="acme") == []
    assert audit.events("nobody") == [] and audit.head("nobody") == GENESIS


def test_events_round_trip_exactly_and_chain_continues_after_reopening(
    pg: PostgresBackend, audit: AuditLog
) -> None:
    add(audit, "acme", 2)
    reopened = AuditLog(PostgresAuditSink(pg))  # a new process attaching to the same database
    add(reopened, "acme", 1)
    events = reopened.events("acme")
    assert [e.seq for e in events] == [0, 1, 2] and reopened.verify(tenant_id="acme") == []
    assert events[0].details == {"i": "0"} and events[0].ts.tzinfo is not None


def test_a_tenant_is_required(audit: AuditLog) -> None:
    with pytest.raises(ValueError, match="belong to a tenant"):
        audit.append("alice", "x.y", "t", source="test")
    with pytest.raises(ValueError, match="belong to a tenant"):
        audit.events()


def test_tampering_is_detected_even_by_someone_who_defeats_the_immutability_trigger(
    pg_db: PgDb, audit: AuditLog
) -> None:
    add(audit, "acme", 4)
    head = audit.head("acme")
    with admin(pg_db) as c:
        c.execute(
            "ALTER TABLE audit_events DISABLE TRIGGER audit_events_immutable"
        )  # an insider with owner rights
        c.execute("UPDATE audit_events SET event = jsonb_set(event, '{actor}', '\"mallory\"') WHERE seq = 1")
        assert any("edited" in p for p in audit.verify(tenant_id="acme"))
        c.execute("UPDATE audit_events SET event = jsonb_set(event, '{actor}', '\"alice\"') WHERE seq = 1")
        assert audit.verify(tenant_id="acme") == []  # restored: the chain is exact again
        c.execute("DELETE FROM audit_events WHERE seq = 2")
        assert audit.verify(tenant_id="acme")  # a removed middle event breaks sequence and links
    with admin(pg_db) as c:
        c.execute("DELETE FROM audit_events WHERE seq >= 2")
    assert audit.verify(tenant_id="acme") == []  # truncation alone is self-consistent...
    assert any(
        "head" in p for p in audit.verify(expected_head=head, tenant_id="acme")
    )  # ...unless the head was kept elsewhere


def test_events_cannot_be_changed_or_deleted_even_by_the_superuser(pg_db: PgDb, audit: AuditLog) -> None:
    add(audit, "acme", 2)
    with admin(pg_db) as c:
        for sql in ("UPDATE audit_events SET actor = 'x'", "DELETE FROM audit_events"):
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="immutable"):
                c.execute(sql)
        with pytest.raises(psycopg.errors.UniqueViolation):  # a fork at an existing position is impossible
            c.execute(
                "INSERT INTO audit_events SELECT chain_id, seq, ts, actor, action, target, project_id, hash, event "
                "FROM audit_events WHERE seq = 1"
            )


def test_row_level_security_and_privileges_on_the_audit_table(pg_db: PgDb, audit: AuditLog) -> None:
    add(audit, "acme", 2)
    add(audit, "globex", 3)
    with psycopg.connect(pg_db.app_dsn, autocommit=True) as c:
        c.execute("SELECT set_config('evalry.tenant_id', 'globex', false)")
        assert c.execute("SELECT count(*) FROM audit_events").fetchone() == (3,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO audit_events SELECT 'acme', 99, ts, actor, action, target, project_id, hash, event FROM audit_events"
            )
        for sql in (
            "UPDATE audit_events SET actor = 'x'",
            "DELETE FROM audit_events",
            "TRUNCATE audit_events",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
        c.execute("SELECT set_config('evalry.tenant_id', '', false)")
        assert c.execute("SELECT count(*) FROM audit_events").fetchone() == (0,)


def test_concurrent_writers_in_separate_processes_never_fork_or_skip(
    pg_db: PgDb, pg: PostgresBackend
) -> None:
    second = PostgresBackend(pg_db.app_dsn)  # a second pool stands in for a second API process
    logs = [AuditLog(PostgresAuditSink(pg)), AuditLog(PostgresAuditSink(second))]
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(6):
                add(logs[n % 2], "acme" if i % 3 else "globex", 1, actor=f"w{n}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    second.close()
    assert errors == []
    for tenant, expected in (("acme", 8 * 4), ("globex", 8 * 2)):
        events = logs[0].events(tenant)
        assert [e.seq for e in events] == list(range(expected)) and logs[0].verify(tenant_id=tenant) == []


def test_store_events_land_in_the_right_tenant_chain(
    pg_db: PgDb, pg: PostgresBackend, audit: AuditLog
) -> None:
    store = Store(audit, pg)
    store.bootstrap_tenant("acme", "acme-admin", operator="ops")
    store.bootstrap_tenant("globex", "globex-admin", operator="ops")
    store.view(Principal("acme-admin", "acme")).create_project("app")
    with pytest.raises(NotFound):
        store.view(Principal("globex-admin", "globex")).list_runs("app")
    acme, globex = audit.events("acme"), audit.events("globex")
    assert [e.action for e in acme] == ["tenant.created", "project.created"]
    assert [e.action for e in globex] == ["tenant.created", "access.denied"]
    assert {e.tenant_id for e in acme} == {"acme"} and {e.tenant_id for e in globex} == {"globex"}
    assert audit.verify(tenant_id="acme") == [] and audit.verify(tenant_id="globex") == []


def test_file_sink_still_works_unchanged(tmp_path: Path) -> None:
    a = AuditLog(tmp_path / "a.jsonl")
    add(a, "acme", 2)
    assert [e.seq for e in a.events()] == [0, 1] and a.verify() == []
