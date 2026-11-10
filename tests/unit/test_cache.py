from pathlib import Path
from typing import Any

import pytest

import evalry.graders
from evalry.cache import GradeCache
from evalry.cli import main
from evalry.engine import grade
from evalry.schemas import GraderScore, Task

T = Task(id="t", language="python", prompt="p", tests="assert f() == 1")
GOOD = "def f(): return 1"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = evalry.graders.python_tests

    def counting(task: Task, code: str, **kw: Any) -> tuple[GraderScore, bool]:
        seen.append(code)
        return real(task, code, **kw)

    monkeypatch.setattr("evalry.engine.python_tests", counting)
    return seen


def test_second_grade_is_a_hit_with_identical_result(tmp_path: Path, calls: list[str]) -> None:
    cache = GradeCache(tmp_path)
    first, second = grade(T, GOOD, cache=cache), grade(T, GOOD, cache=cache)
    assert first == second and first.composite == 1.0
    assert len(calls) == 1 and (cache.hits, cache.misses) == (1, 1)


def test_key_changes_with_code_and_tests() -> None:
    other = Task(id="t", language="python", prompt="p", tests="assert f() == 2")
    keys = {GradeCache.key(T, GOOD), GradeCache.key(T, GOOD + "\n"), GradeCache.key(other, GOOD)}
    assert len(keys) == 3 and GradeCache.key(T, GOOD) == GradeCache.key(T, GOOD)


def test_timeouts_are_not_cached(tmp_path: Path, calls: list[str]) -> None:
    cache = GradeCache(tmp_path)
    assert grade(T, "while True: pass", timeout_s=2, cache=cache).composite == 0.0
    assert list(tmp_path.iterdir()) == []


def test_corrupt_entry_is_a_miss_and_is_replaced(tmp_path: Path, calls: list[str]) -> None:
    cache = GradeCache(tmp_path)
    (tmp_path / f"{GradeCache.key(T, GOOD)}.json").write_text("{not json")
    assert grade(T, GOOD, cache=cache).composite == 1.0 and len(calls) == 1
    assert grade(T, GOOD, cache=cache).composite == 1.0 and len(calls) == 1  # now a hit


def test_cli_suite_cache_across_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    golden = Path(__file__).parent.parent / "golden"
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "fizzbuzz.yaml").write_text((golden / "fizzbuzz.yaml").read_text())
    replies = tmp_path / "r.json"
    good = (golden / "solutions/good.txt").read_text()
    replies.write_text('{"fizzbuzz": [%s, %s]}' % ((__import__("json").dumps(good),) * 2))
    args = [
        "suite",
        str(suite),
        "--replies",
        str(replies),
        "--samples",
        "2",
        "--cache-dir",
        str(tmp_path / "c"),
    ]
    assert main(args) == 0
    assert "1 hits, 1 misses" in capsys.readouterr().out  # sample 1 reuses sample 0's grade
    assert main(args) == 0
    assert "2 hits, 0 misses" in capsys.readouterr().out
