from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalry.generation import FakeGenerator
from evalry.jobs import Job, JobQueue, ProjectLimits, State
from evalry.schemas import SampleResult, Task, load_task
from evalry.worker import collect, sandbox_executor, submit_run, work

GOLDEN = Path(__file__).parent.parent / "golden"
TASKS = [Task(id=f"t{i}", language="python", prompt="p", tests="t") for i in range(3)]
BY_ID = {t.id: t for t in TASKS}
NOW = datetime(2026, 11, 10, tzinfo=UTC)


def submit(q: JobQueue, samples: int = 2) -> list[Job]:
    return submit_run(q, TASKS, project_id="p", suite_version="s1", generator_hash="g", samples=samples)


class Counting:
    """Fake executor: counts executions per job and charges $1 each."""

    def __init__(self, crash_on: int | None = None, fail_task: str | None = None) -> None:
        self.runs: Counter[str] = Counter()
        self.crash_on, self.fail_task, self.total = crash_on, fail_task, 0

    def __call__(self, job: Job, task: Task) -> SampleResult:
        self.total += 1
        if self.crash_on == self.total:
            raise KeyboardInterrupt  # simulates a killed worker: not an Exception, so the job stays RUNNING
        if task.id == self.fail_task:
            raise RuntimeError("provider down")
        self.runs[job.key] += 1
        return SampleResult(
            task_id=task.id,
            task_hash="h",
            sample=job.sample,
            generator_model="m",
            code="c",
            scores=[],
            composite=1.0,
            cost_usd=1.0,
        )


def test_run_completes_and_collects() -> None:
    q, ex = JobQueue(), Counting()
    jobs = submit(q)
    assert work(q, BY_ID, ex) == 6
    rec = collect(q, jobs, run_id="r", suite_version="s1", started_at=NOW)
    assert len(rec.results) == 6 and q.spent("p") == 6.0


def test_resume_after_crash_never_reruns_or_recharges_finished_jobs() -> None:
    q = JobQueue()
    jobs = submit(q)
    crashing = Counting(crash_on=4)
    with pytest.raises(KeyboardInterrupt):
        work(q, BY_ID, crashing)
    assert (
        sorted(j.state.value for j in jobs).count("done") == 3
        and sum(j.state is State.RUNNING for j in jobs) == 1
    )

    assert q.recover() == 1
    resumed = Counting()
    submit(q)  # the resumed run resubmits every job; existing keys are no-ops
    assert work(q, BY_ID, resumed) == 3  # the crashed job plus the two never started
    all_runs = crashing.runs + resumed.runs
    assert len(all_runs) == 6 and set(all_runs.values()) == {1}  # every job completed exactly once
    assert q.spent("p") == 6.0  # one charge per job, not 7
    assert len(collect(q, jobs, run_id="r", suite_version="s1", started_at=NOW).results) == 6


def test_resubmitting_a_finished_run_executes_nothing() -> None:
    q = JobQueue()
    submit(q)
    work(q, BY_ID, Counting())
    again = Counting()
    submit(q)
    assert work(q, BY_ID, again) == 0 and again.total == 0 and q.spent("p") == 6.0


def test_failing_task_retries_then_fails_and_collect_refuses() -> None:
    q, ex = JobQueue(), Counting(fail_task="t1")
    jobs = submit(q, samples=1)
    work(q, BY_ID, ex)
    bad = next(j for j in jobs if j.task_id == "t1")
    assert (bad.state, bad.attempts, "provider down" in bad.error) == (State.FAILED, 3, True)
    assert [j.state for j in jobs if j.task_id != "t1"] == [State.DONE, State.DONE]
    with pytest.raises(ValueError, match="1 of 3 jobs are not done"):
        collect(q, jobs, run_id="r", suite_version="s1", started_at=NOW)


def test_budget_stops_the_run_midway() -> None:
    q = JobQueue({("local", "p"): ProjectLimits(budget_usd=2.0)})
    jobs = submit(q, samples=1)
    ex = Counting()
    assert work(q, BY_ID, ex) == 2  # $1 each; the third job is held back
    assert [j.state for j in jobs].count(State.QUEUED) == 1 and len(q.blocked_by_budget("p")) == 1


def test_max_jobs_limits_one_call() -> None:
    q = JobQueue()
    submit(q)
    assert work(q, BY_ID, Counting(), max_jobs=2) == 2


def test_sandbox_executor_end_to_end_offline() -> None:
    task = load_task(GOLDEN / "fizzbuzz.yaml")
    good = (GOLDEN / "solutions/good.txt").read_text()
    q = JobQueue()
    jobs = submit_run(q, [task], project_id="p", suite_version="s1", generator_hash="g", samples=2)
    ex = sandbox_executor(lambda t, sample: FakeGenerator([good if sample == 0 else "nope"]))
    work(q, {task.id: task}, ex)
    assert [j.result.composite for j in sorted(jobs, key=lambda j: j.sample) if j.result] == [1.0, 0.0]
