from pathlib import Path

import pytest

from evalry.cli import run_sample
from evalry.generation import FakeGenerator, extract_code
from evalry.schemas import load_task

GOLDEN = Path(__file__).parent.parent / "golden"
GOOD = (GOLDEN / "solutions/good.txt").read_text()


def test_extract_code_cases() -> None:
    assert extract_code("```python\nx = 1\n```") == "x = 1\n"
    assert extract_code("draft:\n```\na\n```\nfinal:\n```python\nb\n```\nthanks") == "b\n"  # last block wins
    assert extract_code("x = 1\n") == "x = 1"  # no fence: whole reply
    assert extract_code("") == ""
    assert extract_code("```python\nunterminated") == ""


def test_fake_generator_scripts_and_runs_out() -> None:
    g = FakeGenerator(["a", "b"])
    assert [g.generate("p", sample=i).text for i in range(2)] == ["a", "b"]
    with pytest.raises(RuntimeError, match="no"):
        g.generate("p", sample=2)


def test_run_sample_end_to_end_offline_and_tests_stay_hidden() -> None:
    task = load_task(GOLDEN / "fizzbuzz.yaml")
    gen = FakeGenerator([f"Here you go:\n```python\n{GOOD}```", "I cannot help with that."], model="fake-v2")
    ok, bad = run_sample(task, gen, 0), run_sample(task, gen, 1)
    assert (ok.composite, ok.generator_model, ok.sample) == (1.0, "fake-v2", 0)
    assert (bad.composite, bad.sample) == (0.0, 1)
    assert all(task.tests not in p for p in gen.prompts) and gen.prompts[0] == task.prompt


def test_generation_logging_has_no_text(capfd: pytest.CaptureFixture[str]) -> None:
    run_sample(load_task(GOLDEN / "fizzbuzz.yaml"), FakeGenerator(["SECRET_REPLY_TEXT"]), 0)
    out = capfd.readouterr()
    assert "SECRET_REPLY_TEXT" not in out.out + out.err
    assert "fake-model-1" in out.out + out.err
