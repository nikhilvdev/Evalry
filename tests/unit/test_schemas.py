from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from evalry.log import get_logger
from evalry.schemas import RunRecord, Task, load_task

TASK_YAML = """
id: fizzbuzz
language: python
prompt: Write fizzbuzz(n).
tests: assert fizzbuzz(3) == 'Fizz'
"""


def test_load_task_defaults(tmp_path: Path) -> None:
    p = tmp_path / "t.yaml"
    p.write_text(TASK_YAML)
    t = load_task(p)
    assert (t.tier, t.sensitivity, t.schema_version) == (1, "internal", "1")


def test_task_rejects_unknown_field_and_missing_tests() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate({"id": "a", "language": "python", "prompt": "p", "tests": "t", "surprise": 1})
    with pytest.raises(ValidationError):
        Task(id="a", language="python", prompt="p", tests="")


def test_run_record_roundtrip() -> None:
    rr = RunRecord(run_id="r1", suite_version="s1", started_at=datetime(2026, 11, 10, tzinfo=UTC), results=[])
    assert RunRecord.model_validate_json(rr.model_dump_json()) == rr


def test_logger_redacts_secret() -> None:
    rec = get_logger("test").info("login", password="hunter2")
    assert "hunter2" not in str(rec)
