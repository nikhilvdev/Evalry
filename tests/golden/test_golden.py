"""Saved solutions with known scores. A score diff fails CI (plan §18); update expected.json only on purpose."""

import json
from pathlib import Path

import pytest

from evalry.cli import grade
from evalry.schemas import load_task

HERE = Path(__file__).parent
EXPECTED = json.loads((HERE / "expected.json").read_text())


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_golden_score(name: str) -> None:
    result = grade(load_task(HERE / "fizzbuzz.yaml"), (HERE / "solutions" / f"{name}.txt").read_text())
    assert result.composite == EXPECTED[name]
