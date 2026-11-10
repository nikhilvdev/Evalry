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
    rec = run_suite(load_suite(SUITE), lambda t: FakeGenerator(replies[t.id]), samples=3, suite_version="s1")
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
            lambda t: FakeGenerator([]),
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
