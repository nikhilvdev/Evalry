import json
import time
from pathlib import Path

import psycopg
import pytest

from evalry.cli import main
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


def test_worker_finds_the_suite_from_the_job_and_runs_are_tenant_scoped(db: PgDb) -> None:
    tasks = [Task(id="t", language="python", prompt="p", tests="assert True")]
    pg = PostgresBackend(db.app_dsn)
    try:
        assert pg.put_suite("acme", "app", "s1", tasks, "alice")
    finally:
        pg.close()
    enqueue(db, job("t", run_id="r1", suite_version="s1"))
    w = worker(db)
    try:
        claimed = w.claim()
        assert claimed and (claimed.run_id, claimed.suite_version) == ("r1", "s1")
        assert [
            t.id for t in w.suite_tasks(claimed.tenant_id, claimed.project_id, claimed.suite_version)
        ] == ["t"]
        with pytest.raises(KeyError):
            w.suite_tasks("acme", "app", "missing")
        assert w.run_jobs("r1")[0].key == claimed.key
    finally:
        w.close()
    api = PostgresJobQueue(db.app_dsn, tenant_id="-")
    try:
        assert [j.key for j in api.for_tenant("acme").run_jobs("r1")] == [claimed.key]
        assert api.for_tenant("globex").run_jobs("r1") == []  # row-level security, not just a filter
    finally:
        api.close()


def test_cli_worker_grades_a_queued_run_in_the_sandbox(
    db: PgDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-side enqueue, then `evalry worker --once` as the evalry_worker role, then the API-side assembly of the run."""
    task = Task(
        id="double", language="python", prompt="p", tests="assert double(2) == 4\nassert double(0) == 0"
    )
    pg = PostgresBackend(db.app_dsn)
    try:
        pg.put_suite("acme", "app", "s1", [task], "alice")
    finally:
        pg.close()
    enqueue(db, job("double", run_id="r1", suite_version="s1"))
    replies = tmp_path / "replies.json"
    replies.write_text(json.dumps({"double": ["```python\ndef double(n):\n    return n * 2\n```"]}))
    monkeypatch.setenv("EVALRY_DB_WORKER_DSN", db.worker_dsn)
    assert main(["worker", "--once", "--replies", str(replies)]) == 0
    api = PostgresJobQueue(db.app_dsn, tenant_id="acme")
    try:
        (done,) = api.run_jobs("r1")
    finally:
        api.close()
    assert (
        done.state is State.DONE
        and done.result
        and done.result.composite == 1.0
        and done.result.isolation == "rpc"
    )
    monkeypatch.delenv("EVALRY_DB_WORKER_DSN")
    assert (
        main(["worker", "--once", "--replies", str(replies)]) == 2
    )  # no DSN: refuses with a message, not a traceback


def test_calibrations_are_append_only_and_tenant_scoped(db: PgDb) -> None:
    from datetime import UTC, datetime

    from evalry.judge.calibration import CalibrationRecord

    rec = CalibrationRecord(
        judge_version="jv", set_hash="a" * 64, items=20, agreement_within_one=1, stability=1, injection_resistant=True,
        passed=True, calibration_id="c1", created_at=datetime.now(UTC), created_by="alice",
    )  # fmt: skip
    pg = PostgresBackend(db.app_dsn)
    try:
        assert pg.put_calibration("acme", "app", rec) and not pg.put_calibration("acme", "app", rec)
        assert [r.calibration_id for r in pg.list_calibrations("acme", "app")] == ["c1"]
        assert pg.list_calibrations("globex", "app") == []
        with psycopg.connect(db.app_dsn) as conn:
            conn.execute("SELECT set_config('evalry.tenant_id', 'acme', false)")
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.IntegrityConstraintViolation)
            ):  # no grant, and a trigger behind it
                conn.execute("UPDATE judge_calibrations SET passed = false")
    finally:
        pg.close()


def test_labeling_rows_are_append_only_tenant_scoped_and_one_label_per_reviewer(db: PgDb) -> None:
    from datetime import UTC, datetime

    from evalry import labeling

    now = datetime.now(UTC)
    batch = labeling.LabelBatch(
        batch_id="b1", guideline="g", created_by="boss", created_at=now,
        items=labeling.make_items([labeling.LabelItemIn(prompt="p", code="c")], 0),
    )  # fmt: skip
    label = labeling.Label(batch_id="b1", item_id=0, reviewer="ann", score=4, created_at=now)
    pg = PostgresBackend(db.app_dsn)
    try:
        assert pg.put_label_batch("acme", "app", batch) and not pg.put_label_batch("acme", "app", batch)
        assert pg.put_label("acme", "app", label) and not pg.put_label("acme", "app", label)
        assert [x.score for x in pg.list_labels("acme", "app", "b1")] == [4]
        assert pg.get_label_batch("acme", "app", "b1") == batch
        assert (
            pg.get_label_batch("globex", "app", "b1") is None and pg.list_labels("globex", "app", "b1") == []
        )
        with psycopg.connect(db.app_dsn) as conn:
            conn.execute("SELECT set_config('evalry.tenant_id', 'acme', false)")
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.IntegrityConstraintViolation)
            ):
                conn.execute("UPDATE labels SET reviewer = 'mallory'")  # no grant, and a trigger behind it
    finally:
        pg.close()


def test_dispute_rows_need_a_real_decision_and_are_append_only_and_tenant_scoped(db: PgDb) -> None:
    from datetime import UTC, datetime

    from evalry.disputes import Dispute, Resolution
    from evalry.gates.hard import GateDecision
    from evalry.schemas import RunRecord

    now = datetime.now(UTC)
    run = RunRecord(run_id="r1", suite_version="s1", started_at=now, results=[])
    decision = GateDecision(
        decision_id="d1", gate="g", rule_hash="h", run_id="r1", suite_version="s1", environment="dev", outcome="block",
        reasons=["x"], waived=[], pass_rate=0.0, samples=1, actor="a", decided_at=now,
    )  # fmt: skip
    dispute = Dispute(
        decision_id="d1", run_id="r1", opened_by="ann", reason="why", task_ids=["t"], created_at=now
    )
    res = Resolution(decision_id="d1", resolved_by="boss", outcome="rejected", rationale="no", created_at=now)
    pg = PostgresBackend(db.app_dsn)
    try:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            pg.put_dispute("acme", "app", dispute)  # no such decision yet
        pg.put_run("acme", "app", run)
        pg.put_decision("acme", "app", decision)
        assert pg.put_dispute("acme", "app", dispute) and not pg.put_dispute("acme", "app", dispute)
        assert pg.get_resolution("acme", "app", "d1") is None
        assert pg.put_resolution("acme", "app", res) and not pg.put_resolution("acme", "app", res)
        assert (
            pg.get_dispute("acme", "app", "d1") == dispute and pg.get_resolution("acme", "app", "d1") == res
        )
        assert pg.list_disputes("acme", "app") == [dispute] and pg.list_disputes("globex", "app") == []
        assert pg.get_dispute("globex", "app", "d1") is None
        with psycopg.connect(db.app_dsn) as conn:
            conn.execute("SELECT set_config('evalry.tenant_id', 'acme', false)")
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.IntegrityConstraintViolation)
            ):
                conn.execute("UPDATE dispute_resolutions SET decision_id = 'x'")
    finally:
        pg.close()


def test_production_rows_are_idempotent_append_only_and_tenant_scoped(db: PgDb) -> None:
    from datetime import UTC, datetime, timedelta

    from evalry.monitoring import Closure, Incident, Signal

    now = datetime.now(UTC)
    sig = Signal(signal_id="s1", task_id="t", kind="reverted", observed_at=now)
    inc = Incident(incident_id="i1", task_id="t", kind="other", note="n", opened_by="boss", created_at=now)
    closed = Closure(
        incident_id="i1", outcome="not_an_eval_problem", rationale="r", suite_version=None, added_task_ids=[],
        closed_by="boss", created_at=now,
    )  # fmt: skip
    pg = PostgresBackend(db.app_dsn)
    try:
        assert pg.put_signals("acme", "app", [sig, sig.model_copy(update={"signal_id": "s2"})]) == 2
        assert pg.put_signals("acme", "app", [sig]) == 0  # resending is harmless
        since = now - timedelta(days=1)
        assert (
            len(pg.list_signals("acme", "app", since)) == 2 and pg.list_signals("globex", "app", since) == []
        )
        assert pg.list_signals("acme", "app", now + timedelta(days=1)) == []
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            pg.put_closure("acme", "app", closed)  # no such incident yet
        assert pg.put_incident("acme", "app", inc) and not pg.put_incident("acme", "app", inc)
        assert pg.put_closure("acme", "app", closed) and not pg.put_closure("acme", "app", closed)
        assert pg.list_incidents("acme", "app") == [inc] and pg.list_closures("acme", "app") == [closed]
        assert pg.list_incidents("globex", "app") == []
        with psycopg.connect(db.app_dsn) as conn:
            conn.execute("SELECT set_config('evalry.tenant_id', 'acme', false)")
            with pytest.raises(
                (psycopg.errors.InsufficientPrivilege, psycopg.errors.IntegrityConstraintViolation)
            ):
                conn.execute("UPDATE production_signals SET kind = 'accepted'")
    finally:
        pg.close()
