import json
from pathlib import Path

import pytest

from evalry.audit import GENESIS, AuditLog
from evalry.jobs import Job, JobQueue
from evalry.schemas import SampleResult, Task
from evalry.worker import submit_run, work

TASKS = [Task(id=f"t{i}", language="python", prompt="p", tests="t") for i in range(3)]
BY_ID = {t.id: t for t in TASKS}


def execute(job: Job, task: Task) -> SampleResult:
    if task.id == "t1":
        raise RuntimeError("provider down")
    return SampleResult(
        task_id=task.id, task_hash="h", sample=0, generator_model="m", code="c", scores=[], composite=1
    )


def file_of(a: AuditLog) -> Path:
    assert a.path is not None  # these tests tamper with the local file sink directly
    return a.path


def make(tmp_path: Path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def fill(a: AuditLog, n: int = 3) -> None:
    for i in range(n):
        a.append("alice", "run.submitted", f"p/s{i}", source="cli", project_id="p")


def test_chain_links_and_verifies(tmp_path: Path) -> None:
    a = make(tmp_path)
    fill(a)
    ev = a.events()
    assert [e.seq for e in ev] == [0, 1, 2] and ev[0].prev_hash == GENESIS and ev[1].prev_hash == ev[0].hash
    assert a.verify() == [] and a.verify(expected_head=a.head()) == []


def test_reopening_continues_the_chain(tmp_path: Path) -> None:
    fill(make(tmp_path), 2)
    b = make(tmp_path)
    b.append("bob", "gate.approved", "g1", source="api")
    assert [e.seq for e in b.events()] == [0, 1, 2] and b.verify() == []


def test_edit_is_detected(tmp_path: Path) -> None:
    a = make(tmp_path)
    fill(a)
    lines = file_of(a).read_text().splitlines()
    lines[1] = json.dumps({**json.loads(lines[1]), "actor": "mallory"})
    file_of(a).write_text("\n".join(lines) + "\n")
    assert any("edited" in p for p in a.verify())


def test_deleted_middle_event_is_detected(tmp_path: Path) -> None:
    a = make(tmp_path)
    fill(a)
    lines = file_of(a).read_text().splitlines()
    file_of(a).write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert a.verify()


def test_truncation_needs_the_external_head(tmp_path: Path) -> None:
    a = make(tmp_path)
    fill(a)
    head = a.head()
    lines = file_of(a).read_text().splitlines()
    file_of(a).write_text("\n".join(lines[:2]) + "\n")
    assert a.verify() == []  # a shorter chain is still self-consistent
    assert any("head" in p for p in a.verify(expected_head=head))


def test_content_in_details_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ids or hashes"):
        make(tmp_path).append("a", "x.y", "t", source="cli", details={"code": "x" * 300})


def test_unreadable_log_refuses_to_open(tmp_path: Path) -> None:
    p = tmp_path / "audit.jsonl"
    p.write_text("{not json}\n")
    with pytest.raises(ValueError, match="unreadable"):
        AuditLog(p)


def test_mirror_to_logquill_is_hash_chained_and_omits_details(
    capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    make(tmp_path).append("alice", "run.submitted", "p/s", source="cli", details={"password": "hunter2"})
    out = capfd.readouterr().out
    assert (
        '"message":"run.submitted"' in out and '"hash"' in out and "hunter2" not in out
    )  # details stay in the audit file only


def test_worker_records_run_and_job_events(tmp_path: Path) -> None:
    a, q = make(tmp_path), JobQueue()
    submit_run(
        q, TASKS, project_id="p", suite_version="s1", generator_hash="g", samples=1, audit=a, actor="alice"
    )
    work(q, BY_ID, execute, audit=a)
    actions = [e.action for e in a.events()]
    assert (
        actions[0] == "run.submitted"
        and actions.count("job.completed") == 2
        and actions.count("job.failed") == 3
    )
    assert a.events()[0].actor == "alice" and a.verify() == []
