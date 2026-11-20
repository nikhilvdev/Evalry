"""Database-level guarantees, tested with raw SQL so they hold even if the Python layer has a bug."""

import threading
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from evalry.gates.hard import GateDecision
from evalry.schemas import GraderScore, RunRecord, SampleResult
from evalry.storage import db as dbmod
from evalry.storage.db import MigrationError, migrate
from evalry.storage.pgstore import PostgresBackend, UnsafeConnection
from tests.conftest import PgDb

NOW = datetime(2026, 11, 10, 12, 30, 45, 123456, tzinfo=UTC)


def run(run_id: str = "r1", composite: float = 0.30000000000000004) -> RunRecord:
    return RunRecord(
        run_id=run_id, suite_version="s1", started_at=NOW,
        results=[SampleResult(
            task_id="tâche-ü", task_hash="h", sample=0, generator_model="m", code="print('héllo')\n", composite=composite,
            scores=[GraderScore(grader="tests", grader_version="2", score=1 / 3, evidence="é")], cost_usd=1e-7, seed=0,
        )],
    )  # fmt: skip


def decision(decision_id: str = "d1", run_id: str = "r1") -> GateDecision:
    return GateDecision(
        decision_id=decision_id, gate="g", rule_hash="h", run_id=run_id, suite_version="s1", environment="production",
        outcome="pass", reasons=[], waived=[], pass_rate=1.0, samples=1, actor="ci", decided_at=NOW, signature="sig",
    )  # fmt: skip


@pytest.fixture
def pg(pg_db: PgDb) -> PostgresBackend:
    backend = PostgresBackend(pg_db.app_dsn, admin_dsn=pg_db.admin_dsn)
    for tenant in ("acme", "globex"):
        assert backend.create_tenant(tenant, f"{tenant}-admin")
        assert backend.create_project(tenant, "app")
    return backend


def app_conn(pg_db: PgDb, tenant: str | None) -> psycopg.Connection:
    conn = psycopg.connect(pg_db.app_dsn, autocommit=True)
    if tenant is not None:
        conn.execute("SELECT set_config('evalry.tenant_id', %s, false)", (tenant,))
    return conn


# -- migrations ---------------------------------------------------------------------------------------------------------


def test_migrate_is_idempotent_and_records_a_checksum(pg_db: PgDb) -> None:
    assert migrate(pg_db.admin_dsn) == []  # the fixture already applied everything
    with psycopg.connect(pg_db.admin_dsn) as c:
        rows = c.execute("SELECT version, length(checksum) FROM schema_migrations").fetchall()
    assert rows == [
        ("0001_init.sql", 64),
        ("0002_audit.sql", 64),
        ("0003_jobs.sql", 64),
        ("0004_suites.sql", 64),
        ("0005_job_run.sql", 64),
        ("0006_calibrations.sql", 64),
        ("0007_labeling.sql", 64),
        ("0008_disputes.sql", 64),
        ("0009_production.sql", 64),
    ]


def test_editing_an_applied_migration_is_refused(
    pg_db: PgDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "migrations"
    copy.mkdir()
    for f in dbmod.MIGRATIONS.glob("*.sql"):
        (copy / f.name).write_text(f.read_text())
    (copy / "0001_init.sql").write_text((copy / "0001_init.sql").read_text() + "\n-- edited\n")
    monkeypatch.setattr(dbmod, "MIGRATIONS", copy)
    with pytest.raises(MigrationError, match="edited after it was applied"):
        migrate(pg_db.admin_dsn)


def test_a_database_ahead_of_the_build_is_refused(
    pg_db: PgDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dbmod, "MIGRATIONS", tmp_path)  # a build that knows no migrations
    with pytest.raises(MigrationError, match="does not know"):
        migrate(pg_db.admin_dsn)


# -- row-level security and privileges (raw SQL as the application role) -----------------------------------------------


def test_row_level_security_hides_other_tenants_even_without_a_where_clause(
    pg_db: PgDb, pg: PostgresBackend
) -> None:
    pg.put_run("acme", "app", run("acme-run"))
    pg.put_run("globex", "app", run("globex-run"))
    with app_conn(pg_db, "globex") as c:
        assert c.execute("SELECT run_id FROM runs").fetchall() == [("globex-run",)]
        assert c.execute("SELECT count(*) FROM projects").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM tenant_admins").fetchone() == (1,)
    with app_conn(pg_db, None) as c:  # no tenant bound: nothing is visible
        assert c.execute("SELECT count(*) FROM runs").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM projects").fetchone() == (0,)
    with app_conn(pg_db, "no-such-tenant") as c:
        assert c.execute("SELECT count(*) FROM runs").fetchone() == (0,)


def test_the_application_role_cannot_write_into_another_tenant(pg_db: PgDb, pg: PostgresBackend) -> None:
    with app_conn(pg_db, "globex") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(
                "INSERT INTO runs (tenant_id, project_id, run_id, suite_version, started_at, record) "
                "VALUES ('acme', 'app', 'planted', 's', now(), '{}')"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO project_members VALUES ('acme', 'app', 'mallory', 4)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("INSERT INTO tenant_admins VALUES ('acme', 'mallory')")
    assert pg.list_runs("acme", "app") == [] and not pg.is_tenant_admin("acme", "mallory")


def test_the_application_role_cannot_touch_tenants_or_rewrite_history(
    pg_db: PgDb, pg: PostgresBackend
) -> None:
    pg.put_run("acme", "app", run())
    with app_conn(pg_db, "acme") as c:
        for sql in (
            "SELECT * FROM tenants",
            "INSERT INTO tenants (tenant_id) VALUES ('evil')",
            "UPDATE runs SET suite_version = 'x'",
            "DELETE FROM runs",
            "DELETE FROM projects",
            "UPDATE gate_decisions SET outcome = 'pass'",
            "TRUNCATE runs",
            "DROP TABLE runs",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)


def test_runs_and_decisions_are_immutable_even_for_the_owner(pg_db: PgDb, pg: PostgresBackend) -> None:
    pg.put_run("acme", "app", run())
    pg.put_decision("acme", "app", decision())
    with psycopg.connect(
        pg_db.admin_dsn, autocommit=True
    ) as c:  # superuser: bypasses RLS and privileges, not triggers
        for sql in (
            "UPDATE runs SET suite_version = 'tampered'",
            "DELETE FROM runs",
            "UPDATE gate_decisions SET outcome = 'pass'",
            "DELETE FROM gate_decisions",
        ):
            with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="immutable"):
                c.execute(sql)
    assert pg.get_run("acme", "app", "r1") == run() and pg.get_decision("acme", "app", "d1") == decision()


def test_foreign_keys_are_composite_so_rows_cannot_point_at_another_tenants_project(
    pg_db: PgDb, pg: PostgresBackend
) -> None:
    pg.create_project("acme", "only-acme")
    with psycopg.connect(pg_db.admin_dsn, autocommit=True) as c:  # even as superuser the constraint holds
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute(
                "INSERT INTO runs (tenant_id, project_id, run_id, suite_version, started_at, record) "
                "VALUES ('globex', 'only-acme', 'x', 's', now(), '{}')"
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            c.execute("INSERT INTO project_members VALUES ('globex', 'only-acme', 'x', 1)")
        pg.put_run("acme", "app", run("acme-run"))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):  # a decision cannot cite another tenant's run
            c.execute(
                "INSERT INTO gate_decisions VALUES ('globex', 'app', 'd', 'acme-run', 'pass', 'dev', %s, now())",
                (Jsonb({}),),
            )


def test_check_constraints(pg_db: PgDb, pg: PostgresBackend) -> None:
    with psycopg.connect(pg_db.admin_dsn, autocommit=True) as c:
        for sql in (
            "INSERT INTO project_members VALUES ('acme', 'app', 'x', 9)",
            "INSERT INTO projects (tenant_id, project_id) VALUES ('acme', 'Bad_Name')",
            "INSERT INTO tenants (tenant_id) VALUES ('')",
        ):
            with pytest.raises(psycopg.errors.CheckViolation):
                c.execute(sql)


def test_the_backend_refuses_a_superuser_connection(pg_db: PgDb) -> None:
    with pytest.raises(UnsafeConnection, match="BYPASSRLS"):
        PostgresBackend(pg_db.admin_dsn)


def test_creating_a_tenant_needs_the_admin_connection(pg_db: PgDb) -> None:
    app_only = PostgresBackend(pg_db.app_dsn)
    try:
        with pytest.raises(RuntimeError, match="admin connection"):
            app_only.create_tenant("t", "a")
    finally:
        app_only.close()


# -- behaviour ----------------------------------------------------------------------------------------------------------


def test_records_round_trip_exactly(pg: PostgresBackend) -> None:
    original = run()
    assert pg.put_run("acme", "app", original) and pg.get_run("acme", "app", "r1") == original
    assert pg.put_decision("acme", "app", decision())
    assert not pg.put_run("acme", "app", original) and not pg.put_decision("acme", "app", decision())
    assert pg.get_run("globex", "app", "r1") is None and pg.get_decision("globex", "app", "d1") is None


def test_list_order_matches_the_in_memory_backend(pg: PostgresBackend) -> None:
    ids = ["b", "B", "a", "Z", "_x", "é", "10", "9"]
    for i in ids:
        pg.put_run("acme", "app", run(i))
    assert pg.list_runs("acme", "app") == sorted(ids)


def test_only_one_of_many_concurrent_writers_wins(pg: PostgresBackend) -> None:
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        won = pg.put_run("acme", "app", run("race"))
        with lock:
            results.append(won)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == 12
    creators = [pg.create_project("acme", "same") for _ in range(1)]
    assert creators == [True] and pg.create_project("acme", "same") is False


def test_membership_upsert_and_remove(pg: PostgresBackend) -> None:
    pg.set_member("acme", "app", "dana", 2)
    pg.set_member("acme", "app", "dana", 3)  # changing a role replaces it
    assert pg.member_role("acme", "app", "dana") == 3 and pg.member_role("globex", "app", "dana") is None
    assert pg.remove_member("acme", "app", "dana") == 3 and pg.remove_member("acme", "app", "dana") is None
