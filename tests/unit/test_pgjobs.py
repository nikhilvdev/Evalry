import time

import psycopg
import pytest

from evalry.scheduler.jobs import Job, Priority, ProjectLimits, Stage, State, job_key
from evalry.scheduler.worker import work
from evalry.schemas import SampleResult, Task
from evalry.storage.pgjobs import PostgresJobQueue
from evalry.storage.pgstore import PostgresBackend, UnsafeConnection
from tests.conftest import PgDb


def job(task: str = "t", sample: int = 0, tenant: str = "acme", project: str = "app", **kw: object) -> Job:
    return Job(
        key=job_key(project, "s1", task, sample, Stage.GRADE, "g", tenant),
        tenant_id=tenant, project_id=project, task_id=task, sample=sample, **kw,  # type: ignore[arg-type]
    )  # fmt: skip


def result(task: str = "t", cost: float = 0.0) -> SampleResult:
    return SampleResult(
        task_id=task,
        task_hash="h",
        sample=0,
        generator_model="m",
        code="c",
        scores=[],
        composite=1,
        cost_usd=cost,
    )


@pytest.fixture
def db(pg_db: PgDb) -> PgDb:
    pg = PostgresBackend(pg_db.app_dsn, admin_dsn=pg_db.admin_dsn)
    try:
        for t in ("acme", "globex"):
            pg.create_tenant(t, f"{t}-admin")
            pg.create_project(t, "app")
    finally:
        pg.close()
    return pg_db


def enqueue(db: PgDb, *jobs: Job) -> None:
    """Submit the way production does: through the tenant-bound API role. Workers cannot insert."""
    for tenant in {j.tenant_id for j in jobs}:
        api = PostgresJobQueue(db.app_dsn, tenant_id=tenant)
        try:
            for j in jobs:
                if j.tenant_id == tenant:
                    api.submit(j)
        finally:
            api.close()


def worker(db: PgDb, **kw: object) -> PostgresJobQueue:
    return PostgresJobQueue(db.worker_dsn, **kw)  # type: ignore[arg-type]


def test_submit_is_idempotent_and_priority_then_fifo(db: PgDb) -> None:
    q = worker(db)
    try:
        enqueue(db, job("a"), job("b", priority=Priority.SMOKE), job("c"))
        enqueue(db, job("a"))  # resubmitting is a no-op
        assert len(q.jobs()) == 3
        order = [q.claim() for _ in range(3)]
        assert [j.task_id for j in order if j] == ["b", "a", "c"]
        assert q.claim() is None
    finally:
        q.close()


def test_concurrency_limit_budget_and_charge_once(db: PgDb) -> None:
    q = worker(db, limits={("acme", "app"): ProjectLimits(max_concurrent=1, budget_usd=1.0)})
    try:
        enqueue(db, *(job(t) for t in ("a", "b", "c")))
        first = q.claim()
        assert first is not None and q.claim() is None  # one in flight, the limit holds
        q.complete(first.key, result("a", 1.5))
        q.complete(first.key, result("a", 9.0))  # duplicate completion
        assert q.spent("app", "acme") == 1.5 and q.get(first.key).result is not None
        assert q.claim() is None  # budget spent: the rest never run
        assert {j.task_id for j in q.blocked_by_budget("app", "acme")} == {"b", "c"}
    finally:
        q.close()


def test_retries_then_fails(db: PgDb) -> None:
    q = worker(db)
    try:
        enqueue(db, job("a", max_attempts=2))
        for attempt in (1, 2):
            j = q.claim()
            assert j is not None and j.attempts == attempt
            q.fail(j.key, "boom")
        assert j is not None
        assert q.get(j.key).state is State.FAILED and q.get(j.key).error == "boom" and q.claim() is None
    finally:
        q.close()


def test_a_dead_workers_job_is_requeued_when_its_lease_expires(db: PgDb) -> None:
    q = worker(db, lease_s=0.2)
    try:
        enqueue(db, job("a"))
        assert q.claim() is not None and q.claim() is None
        time.sleep(0.4)  # the worker "crashed": nobody completed or failed it
        again = q.claim()
        assert again is not None and again.attempts == 2
    finally:
        q.close()


def test_tenants_are_isolated_and_only_workers_can_claim(db: PgDb) -> None:
    w = worker(db)
    acme = PostgresJobQueue(db.app_dsn, tenant_id="acme")
    globex = PostgresJobQueue(db.app_dsn, tenant_id="globex")
    try:
        mine = acme.submit(job("a"))
        assert [j.key for j in acme.jobs()] == [mine.key] and globex.jobs() == []
        with pytest.raises(KeyError):
            globex.get(mine.key)
        with pytest.raises(psycopg.Error):  # the API role cannot enqueue into another tenant
            globex.submit(job("x", tenant="acme"))
        with pytest.raises(psycopg.Error):  # nor claim, complete or fail anything
            acme.claim()
        claimed = w.claim()
        assert claimed is not None and claimed.tenant_id == "acme"
        w.complete(claimed.key, result())
        assert acme.get(mine.key).state is State.DONE and globex.jobs() == []
    finally:
        for q in (w, acme, globex):
            q.close()


def test_roles_are_checked_at_connect_time(db: PgDb) -> None:
    with pytest.raises(UnsafeConnection, match="evalry_worker"):
        PostgresJobQueue(db.app_dsn)
    with pytest.raises(UnsafeConnection, match="superuser"):
        PostgresJobQueue(db.admin_dsn, tenant_id="acme")


def test_a_done_job_cannot_be_changed_even_by_the_worker_role(db: PgDb) -> None:
    q = worker(db)
    try:
        enqueue(db, job("a"))
        j = q.claim()
        assert j is not None
        q.complete(j.key, result())
        with (
            psycopg.connect(db.worker_dsn) as c,
            pytest.raises(psycopg.Error, match="finished job never changes"),
        ):
            c.execute("UPDATE jobs SET error = 'x' WHERE key = %s", (j.key,))
    finally:
        q.close()


def test_worker_loop_runs_on_the_durable_queue(db: PgDb) -> None:
    q = worker(db)
    try:
        enqueue(db, job("a"), job("b"))
        tasks = {t: Task(id=t, language="python", prompt="p", tests="assert True") for t in ("a", "b")}
        n = work(q, tasks, lambda j, _t: result(j.task_id, 0.1))
        assert (
            n == 2
            and all(j.state is State.DONE for j in q.jobs())
            and q.spent("app", "acme") == pytest.approx(0.2)
        )
    finally:
        q.close()


def test_workers_can_read_suites_but_not_write_them(db: PgDb) -> None:
    pg = PostgresBackend(db.app_dsn, admin_dsn=db.admin_dsn)
    try:
        pg.put_suite(
            "acme", "app", "v1", [Task(id="a", language="python", prompt="p", tests="assert True")], "alice"
        )
    finally:
        pg.close()
    with psycopg.connect(db.worker_dsn) as c:
        assert c.execute("SELECT count(*) FROM suites").fetchone() == (1,)
        with pytest.raises(psycopg.Error):
            c.execute("DELETE FROM suites")
