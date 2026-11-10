import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from evalry.audit import AuditLog
from evalry.cli import main
from evalry.gates import Environment, GateDecision, GateException, HardGateRule, decide, verify
from evalry.schemas import GraderScore, RunRecord, SampleResult

NOW = datetime(2026, 11, 10, tzinfo=UTC)
KEY = b"test-key"
RULE = HardGateRule(name="smoke", suite_version="s1", grader_version="2")


def result(
    task: str, score: float = 1.0, isolation: Literal["rpc", "inprocess"] | None = "rpc", version: str = "2"
) -> SampleResult:
    return SampleResult(
        task_id=task, task_hash="h", sample=0, generator_model="m", code="c", composite=score,
        scores=[GraderScore(grader="tests", grader_version=version, score=score)], isolation=isolation,
    )  # fmt: skip


def run(*results: SampleResult, suite: str = "s1") -> RunRecord:
    return RunRecord(run_id="r1", suite_version=suite, started_at=NOW, results=list(results))


def go(
    tmp_path: Path,
    record: RunRecord,
    rule: HardGateRule = RULE,
    *,
    key: bytes | None = KEY,
    environment: Environment = "production",
    exceptions: list[GateException] | None = None,
) -> tuple[GateDecision, AuditLog]:
    audit = AuditLog(tmp_path / "audit.jsonl")
    d = decide(
        record, rule, actor="ci", environment=environment, project_id="p", key=key, audit=audit,
        exceptions=exceptions, now=NOW,
    )  # fmt: skip
    return d, audit


def test_all_pass_is_signed_and_audited(tmp_path: Path) -> None:
    d, audit = go(tmp_path, run(result("a"), result("b")))
    assert (d.outcome, d.pass_rate, d.reasons) == ("pass", 1.0, [])
    assert verify(d, KEY) and not verify(d, b"other")
    assert verify(d.model_copy(update={"outcome": "pass"}), KEY)
    assert not verify(d.model_copy(update={"outcome": "block"}), KEY)
    ev = audit.events()
    assert (
        [e.action for e in ev] == ["gate.decided"]
        and ev[0].details["outcome"] == "pass"
        and audit.verify() == []
    )


def test_failing_sample_blocks_and_names_the_task(tmp_path: Path) -> None:
    d, _ = go(tmp_path, run(result("a"), result("b", 0.0)))
    assert d.outcome == "block" and "['b']" in d.reasons[0]


def test_pass_rate_threshold(tmp_path: Path) -> None:
    rule = RULE.model_copy(update={"min_pass_rate": 0.5})
    assert go(tmp_path, run(result("a"), result("b", 0.0)), rule)[0].outcome == "pass"


def test_wrong_suite_version_blocks(tmp_path: Path) -> None:
    d, _ = go(tmp_path, run(result("a"), suite="latest"))
    assert d.outcome == "block" and "approved suite 's1'" in d.reasons[0]


def test_inprocess_or_unrecorded_isolation_cannot_gate(tmp_path: Path) -> None:
    for iso in ("inprocess", None):
        d, _ = go(tmp_path, run(result("a", isolation=iso)))
        assert d.outcome == "block" and "require isolation 'rpc'" in d.reasons[0]


def test_other_grader_version_does_not_count(tmp_path: Path) -> None:
    assert go(tmp_path, run(result("a", version="1")))[0].outcome == "block"


def test_empty_run_and_missing_required_task_block(tmp_path: Path) -> None:
    assert go(tmp_path, run())[0].outcome == "block"
    rule = RULE.model_copy(update={"required_tasks": ["a", "z"]})
    d, _ = go(tmp_path, run(result("a")), rule)
    assert d.outcome == "block" and "['z']" in d.reasons[0]


def exception(task: str = "b", days: int = 7) -> GateException:
    return GateException(
        task_id=task,
        approver="dana",
        reason="flaky upstream, ticket 42",
        expires_at=NOW + timedelta(days=days),
    )


def test_exception_waives_one_task_and_is_logged(tmp_path: Path) -> None:
    d, audit = go(tmp_path, run(result("a"), result("b", 0.0)), exceptions=[exception()])
    assert d.outcome == "pass" and [w.approver for w in d.waived] == ["dana"]
    applied = [e for e in audit.events() if e.action == "gate.exception.applied"]
    assert (
        len(applied) == 1
        and applied[0].actor == "dana"
        and applied[0].details["decision_id"] == d.decision_id
    )


def test_expired_or_wrong_task_exception_does_not_help(tmp_path: Path) -> None:
    assert (
        go(tmp_path, run(result("a"), result("b", 0.0)), exceptions=[exception(days=-1)])[0].outcome
        == "block"
    )
    assert (
        go(tmp_path, run(result("a"), result("b", 0.0)), exceptions=[exception(task="a")])[0].outcome
        == "block"
    )


def test_everything_waived_is_not_a_pass(tmp_path: Path) -> None:
    d, _ = go(tmp_path, run(result("b", 0.0)), exceptions=[exception()])
    assert d.outcome == "block" and "nothing was actually gated" in d.reasons[0]


def test_exception_cannot_waive_isolation(tmp_path: Path) -> None:
    d, _ = go(tmp_path, run(result("b", 0.0, isolation="inprocess")), exceptions=[exception()])
    assert d.outcome == "block"


def test_production_requires_a_key_and_other_environments_are_rehearsals(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be signed"):
        go(tmp_path, run(result("a")), key=None)
    d, audit = go(tmp_path, run(result("a")), key=None, environment="staging")
    assert d.signature == "" and audit.events()[0].action == "gate.rehearsed"


def test_cli_gate_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_file = tmp_path / "run.json"
    run_file.write_text(run(result("a"), result("b", 0.0)).model_dump_json())
    rule = tmp_path / "rule.yaml"
    rule.write_text("name: smoke\nsuite_version: s1\ngrader_version: '2'\n")
    args = [
        "gate",
        str(run_file),
        "--rule",
        str(rule),
        "--audit-log",
        str(tmp_path / "a.jsonl"),
        "--environment",
        "production",
        "--out",
        str(tmp_path / "d.json"),
    ]
    monkeypatch.delenv("EVALRY_GATE_KEY", raising=False)
    assert main(args) == 2 and "must be signed" in capsys.readouterr().err
    monkeypatch.setitem(os.environ, "EVALRY_GATE_KEY", "k")
    assert main(args) == 1 and "BLOCK" in capsys.readouterr().out
    exc = tmp_path / "exc.yaml"
    exc.write_text(
        f"- task_id: b\n  approver: dana\n  reason: known flake\n  expires_at: '{(datetime.now(UTC) + timedelta(days=3)).isoformat()}'\n"
    )
    assert main([*args, "--exceptions", str(exc)]) == 0
    assert (tmp_path / "d.json").exists() and AuditLog(tmp_path / "a.jsonl").verify() == []
