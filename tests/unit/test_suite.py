import json
from pathlib import Path

import pytest

from evalry.cli import main
from evalry.generation import FakeGenerator
from evalry.schemas import RunRecord, Task
from evalry.suite import load_suite, run_suite, summarize

GOLDEN = Path(__file__).parent.parent / "golden"
SUITE = GOLDEN / "suite"
GOOD = (GOLDEN / "solutions/good.txt").read_text()
BAD = "def fizzbuzz(n):\n    return str(n)\n"
DOUBLE = "def double(n):\n    return n * 2\n"


def test_load_suite_sorted_and_rejects_empty_or_duplicates(tmp_path: Path) -> None:
    assert [t.id for t in load_suite(SUITE)] == ["double", "fizzbuzz"]
    with pytest.raises(ValueError, match="No task"):
        load_suite(tmp_path)
    (tmp_path / "a.yaml").write_text("id: x\nlanguage: python\nprompt: p\ntests: t\n")
    (tmp_path / "b.yaml").write_text("id: x\nlanguage: python\nprompt: p\ntests: t\n")
    with pytest.raises(ValueError, match="Duplicate"):
        load_suite(tmp_path)


def test_run_suite_n_samples_and_summary() -> None:
    replies = {"fizzbuzz": [GOOD, BAD, GOOD], "double": [DOUBLE] * 3}
    rec = run_suite(
        load_suite(SUITE), lambda t, i: FakeGenerator([replies[t.id][i]]), samples=3, suite_version="s1"
    )
    assert len(rec.results) == 6 and rec.suite_version == "s1"
    assert sorted((r.task_id, r.sample) for r in rec.results)[:3] == [
        ("double", 0),
        ("double", 1),
        ("double", 2),
    ]
    s = summarize(rec, k=1)
    assert s["double"].stats.mean == 1.0 and s["double"].pass_at_k == 1.0
    assert s["fizzbuzz"].stats.mean == pytest.approx(2 / 3) and s["fizzbuzz"].pass_at_k == pytest.approx(
        2 / 3
    )
    assert summarize(rec, k=5)["double"].pass_at_k is None  # fewer samples than k: no estimate


def test_run_suite_rejects_zero_samples() -> None:
    with pytest.raises(ValueError):
        run_suite(
            [Task(id="a", language="python", prompt="p", tests="t")],
            lambda t, i: FakeGenerator([]),
            samples=0,
            suite_version="s",
        )


def test_cli_suite_writes_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    replies = tmp_path / "r.json"
    replies.write_text(json.dumps({"fizzbuzz": [GOOD, GOOD], "double": [DOUBLE, DOUBLE]}))
    out = tmp_path / "run.json"
    assert (
        main(["suite", str(SUITE), "--replies", str(replies), "--samples", "2", "-k", "2", "--out", str(out)])
        == 0
    )
    assert len(RunRecord.model_validate_json(out.read_text()).results) == 4
    assert "fizzbuzz: mean=1.00" in capsys.readouterr().out


def test_cli_suite_missing_replies_is_a_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    replies = tmp_path / "r.json"
    replies.write_text("{}")
    assert main(["suite", str(SUITE), "--replies", str(replies)]) == 2
    assert "scripted responses" in capsys.readouterr().err


def test_cli_suite_estimate_and_budget_stop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    replies = tmp_path / "r.json"
    replies.write_text("{}")  # would fail if the run started: the budget must stop it first
    rates = tmp_path / "rates.json"
    rates.write_text('{"usd_per_mtok_in": 1000, "usd_per_mtok_out": 1000}')
    assert (
        main(["suite", str(SUITE), "--replies", str(replies), "--rates", str(rates), "--budget", "0.001"])
        == 2
    )
    cap = capsys.readouterr()
    assert "estimated cost: $" in cap.out and "exceeds the budget" in cap.err and "scripted" not in cap.err
    assert main(["suite", str(SUITE), "--replies", str(replies), "--budget", "1"]) == 2
    assert "--rates" in capsys.readouterr().err


def test_log_records_carry_run_context(capfd: pytest.CaptureFixture[str]) -> None:
    run_suite(
        load_suite(SUITE)[:1], lambda t, i: FakeGenerator([DOUBLE]), samples=1, suite_version="ctx-suite"
    )
    out = capfd.readouterr().out
    assert '"suite_version":"ctx-suite"' in out and '"run_id":' in out


def test_cli_suite_audit_log_and_budget(tmp_path: Path) -> None:
    replies = tmp_path / "r.json"
    replies.write_text(json.dumps({"fizzbuzz": [GOOD], "double": [DOUBLE]}))
    audit = tmp_path / "audit.jsonl"
    args = [
        "suite",
        str(SUITE),
        "--replies",
        str(replies),
        "--audit-log",
        str(audit),
        "--actor",
        "alice",
        "--project",
        "pilot",
    ]
    assert main(args) == 0
    from evalry.audit import AuditLog

    log = AuditLog(audit)
    actions = [e.action for e in log.events()]
    assert actions == ["run.submitted", "job.completed", "job.completed"] and log.verify() == []
    assert {e.actor for e in log.events() if e.action == "run.submitted"} == {"alice"}
    assert {e.project_id for e in log.events()} == {"pilot"}
