from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalry.cli import main
from evalry.schemas import RunRecord, SampleResult
from evalry.scoring.compare import min_samples, task_means


def record(scores: dict[str, list[float]], suite: str = "s1") -> RunRecord:
    rs = [
        SampleResult(
            task_id=t, task_hash="h", sample=i, generator_model="m", code="c", scores=[], composite=v
        )
        for t, vs in scores.items()
        for i, v in enumerate(vs)
    ]
    return RunRecord(
        run_id="r", suite_version=suite, started_at=datetime(2026, 11, 10, tzinfo=UTC), results=rs
    )


def write(tmp_path: Path, name: str, rec: RunRecord) -> str:
    p = tmp_path / name
    p.write_text(rec.model_dump_json())
    return str(p)


def test_task_means_and_min_samples() -> None:
    rec = record({"a": [1.0, 0.0], "b": [1.0]})
    assert task_means(rec) == {"a": 0.5, "b": 1.0} and min_samples(rec) == 1


def test_powered_regression_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = write(tmp_path, "b.json", record({f"t{i}": [1.0] * 5 for i in range(100)}))
    cand = write(tmp_path, "c.json", record({f"t{i}": [0.5] * 5 for i in range(100)}))
    assert main(["compare", base, cand]) == 1
    assert "REGRESSION" in capsys.readouterr().out
    assert main(["compare", base, base]) == 0


def test_underpowered_is_advisory_not_a_block(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = write(tmp_path, "b.json", record({"a": [1.0]}))
    cand = write(tmp_path, "c.json", record({"a": [0.0]}))
    assert main(["compare", base, cand]) == 0
    out = capsys.readouterr().out
    assert "delta=-1.000" in out and "advisory only" in out


def test_different_suite_versions_and_bad_files_exit_2(tmp_path: Path) -> None:
    a = write(tmp_path, "a.json", record({"a": [1.0]}, suite="s1"))
    b = write(tmp_path, "b.json", record({"a": [1.0]}, suite="s2"))
    assert main(["compare", a, b]) == 2
    assert main(["compare", a, str(tmp_path / "missing.json")]) == 2
