import pytest

from evalry.jobs import Job, JobQueue, Priority, ProjectLimits, Stage, State, job_key
from evalry.schemas import SampleResult


def key(
    task: str = "t", sample: int = 0, project: str = "p", suite: str = "s1", stage: Stage = Stage.GRADE
) -> str:
    return job_key(project, suite, task, sample, stage, "gen1")


def job(
    task: str = "t",
    sample: int = 0,
    project: str = "p",
    prio: Priority = Priority.NIGHTLY,
    max_attempts: int = 3,
) -> Job:
    return Job(
        key=key(task, sample, project),
        project_id=project,
        task_id=task,
        sample=sample,
        priority=prio,
        max_attempts=max_attempts,
    )


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


def test_key_depends_on_every_input() -> None:
    base = key()
    assert base == key()
    assert (
        len({base, key(task="u"), key(sample=1), key(project="q"), key(suite="s2"), key(stage=Stage.JUDGE)})
        == 6
    )


def test_submit_is_idempotent_and_done_jobs_never_rerun() -> None:
    q = JobQueue()
    first = q.submit(job())
    claimed = q.claim()
    assert claimed is first
    q.complete(first.key, result(cost=1.0))
    again = q.submit(job())  # resumed run resubmits everything
    assert again is first and again.state is State.DONE
    assert q.claim() is None
    q.complete(first.key, result(cost=1.0))  # duplicate completion
    assert q.spent("p") == 1.0


def test_priority_then_fifo() -> None:
    q = JobQueue()
    for j in [job("n1"), job("n2"), job("s1", prio=Priority.SMOKE)]:
        q.submit(j)
    assert [q.claim().task_id for _ in range(3)] == ["s1", "n1", "n2"]  # type: ignore[union-attr]


def test_per_project_concurrency_limit() -> None:
    q = JobQueue({"a": ProjectLimits(max_concurrent=1)})
    for j in [job("a1", project="a"), job("a2", project="a"), job("b1", project="b")]:
        q.submit(j)
    first, second = q.claim(), q.claim()
    assert (first.task_id, second.task_id) == ("a1", "b1")  # type: ignore[union-attr]
    assert q.claim() is None  # a2 waits for a1
    q.complete(first.key, result("a1"))  # type: ignore[union-attr]
    assert q.claim().task_id == "a2"  # type: ignore[union-attr]


def test_retry_then_fail() -> None:
    q = JobQueue()
    j = q.submit(job(max_attempts=2))
    for expected in (State.QUEUED, State.FAILED):
        claimed = q.claim()
        assert claimed is j
        q.fail(j.key, "boom")
        assert j.state is expected
    assert q.claim() is None and j.error == "boom"


def test_budget_stops_new_work_but_not_other_projects() -> None:
    q = JobQueue({"a": ProjectLimits(budget_usd=1.0)})
    for j in [job("a1", project="a"), job("a2", project="a"), job("b1", project="b")]:
        q.submit(j)
    a1 = q.claim()
    q.complete(a1.key, result("a1", cost=1.0))  # type: ignore[union-attr]
    assert q.claim().task_id == "b1"  # type: ignore[union-attr]
    assert q.claim() is None
    assert [j.task_id for j in q.blocked_by_budget("a")] == ["a2"]


def test_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        JobQueue().get("nope")


def test_recover_fails_a_job_that_keeps_killing_workers() -> None:
    q = JobQueue()
    j = q.submit(job(max_attempts=2))
    for expected in (State.QUEUED, State.FAILED):
        assert q.claim() is j
        assert q.recover() == 1 and j.state is expected  # simulated worker death each time
    assert q.claim() is None and "worker died" in j.error
