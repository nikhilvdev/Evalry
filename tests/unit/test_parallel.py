import threading
import time
from pathlib import Path

import pytest

from evalry.audit.chain import AuditLog
from evalry.cli import main
from evalry.generation.base import FakeGenerator
from evalry.scheduler.jobs import Job, JobQueue, ProjectLimits, State
from evalry.scheduler.suite import load_suite, run_suite
from evalry.scheduler.worker import submit_run, work
from evalry.schemas import RunRecord, SampleResult, Task

TASKS = [Task(id=f"t{i}", language="python", prompt="p", tests="t") for i in range(8)]
BY_ID = {t.id: t for t in TASKS}
GOLDEN = Path(__file__).parent.parent / "golden"


class Probe:
    """Executor that records how many jobs ran at once."""

    def __init__(self, delay: float = 0.15, fail_first: set[str] | None = None) -> None:
        self.lock = threading.Lock()
        self.now = self.peak = 0
        self.delay = delay
        self.runs: dict[str, int] = {}
        self.fail_first = fail_first or set()

    def __call__(self, job: Job, task: Task) -> SampleResult:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
            self.runs[job.key] = self.runs.get(job.key, 0) + 1
            first = self.runs[job.key] == 1 and task.id in self.fail_first
        time.sleep(self.delay)
        with self.lock:
            self.now -= 1
        if first:
            raise RuntimeError("transient")
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


def queue_with(limit: int = 8, budget: float = float("inf")) -> JobQueue:
    q = JobQueue({("local", "p"): ProjectLimits(max_concurrent=limit, budget_usd=budget)})
    submit_run(q, TASKS, project_id="p", suite_version="s1", generator_hash="g", samples=1)
    return q


def test_parallel_workers_overlap_and_finish_every_job_once() -> None:
    q, probe = queue_with(), Probe()
    start = time.monotonic()
    assert work(q, BY_ID, probe, workers=4) == 8
    elapsed = time.monotonic() - start
    assert 2 <= probe.peak <= 4 and elapsed < 8 * 0.15 * 0.7  # clearly faster than sequential (1.2 s)
    assert all(j.state is State.DONE for j in q.jobs()) and set(probe.runs.values()) == {1}
    assert q.spent("p") == 8.0


def test_project_concurrency_limit_caps_parallelism() -> None:
    q, probe = queue_with(limit=2), Probe()
    work(q, BY_ID, probe, workers=6)
    assert probe.peak == 2 and all(j.state is State.DONE for j in q.jobs())


def test_failed_job_is_retried_while_other_workers_run() -> None:
    q, probe = queue_with(), Probe(fail_first={"t3"})
    work(q, BY_ID, probe, workers=4)
    assert all(j.state is State.DONE for j in q.jobs())
    assert max(probe.runs.values()) == 2 and sum(v == 2 for v in probe.runs.values()) == 1


def test_budget_overshoot_is_bounded_by_the_workers_in_flight() -> None:
    q, probe = queue_with(limit=3, budget=3.0), Probe(delay=0.05)
    work(q, BY_ID, probe, workers=3)
    done = sum(j.state is State.DONE for j in q.jobs())
    assert (
        3 <= done <= 3 + 2 and len(q.blocked_by_budget("p")) == 8 - done
    )  # budget is checked at claim time only


def test_max_jobs_is_shared_across_workers() -> None:
    assert work(queue_with(), BY_ID, Probe(delay=0.01), workers=4, max_jobs=5) == 5


def test_workers_must_be_positive() -> None:
    with pytest.raises(ValueError):
        work(JobQueue(), {}, Probe(), workers=0)


def test_audit_chain_survives_concurrent_appends(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    q = queue_with()
    work(q, BY_ID, Probe(delay=0.01), audit=audit, workers=6)
    assert len(audit.events()) == 8 and audit.verify() == []


def test_real_sandbox_suite_in_parallel_matches_sequential(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    good = (GOLDEN / "solutions/good.txt").read_text()
    replies = {"fizzbuzz": [good, "nope", good, "nope"], "double": ["def double(n):\n    return n * 2\n"] * 4}
    tasks = load_suite(GOLDEN / "suite")

    def go(workers: int) -> RunRecord:
        return run_suite(
            tasks,
            lambda t, i: FakeGenerator([replies[t.id][i]]),
            samples=4,
            suite_version="par",
            workers=workers,
        )

    seq, par = go(1), go(4)
    key = lambda r: (r.task_id, r.sample)
    assert [(r.task_id, r.sample, r.composite) for r in sorted(par.results, key=key)] == [
        (r.task_id, r.sample, r.composite) for r in sorted(seq.results, key=key)
    ]
    out = capfd.readouterr().out
    assert f'"run_id":"{par.run_id}"' in out  # worker threads kept the run context in their log records


def test_cli_workers_flag(tmp_path: Path) -> None:
    import json

    good = (GOLDEN / "solutions/good.txt").read_text()
    replies = tmp_path / "r.json"
    replies.write_text(
        json.dumps({"fizzbuzz": [good, good], "double": ["def double(n):\n    return n * 2\n"] * 2})
    )
    assert (
        main(["suite", str(GOLDEN / "suite"), "--replies", str(replies), "--samples", "2", "--workers", "4"])
        == 0
    )
