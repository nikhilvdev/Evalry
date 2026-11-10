from pathlib import Path

import pytest

from evalry.cli import main
from evalry.runner import PYTHON_IMAGE, SEED
from evalry.schemas import RunRecord

GOLDEN = Path(__file__).parent.parent / "golden"


def test_run_writes_record_and_exit_codes(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    assert (
        main(
            [
                "run",
                str(GOLDEN / "fizzbuzz.yaml"),
                str(GOLDEN / "solutions/good.txt"),
                "--out",
                str(out),
                "--suite-version",
                "s7",
            ]
        )
        == 0
    )
    rec = RunRecord.model_validate_json(out.read_text())
    r = rec.results[0]
    assert (rec.suite_version, r.composite, r.seed) == ("s7", 1.0, SEED)
    assert r.image == PYTHON_IMAGE and len(r.task_hash) == 64
    assert main(["run", str(GOLDEN / "fizzbuzz.yaml"), str(GOLDEN / "solutions/wrong.txt")]) == 1


def test_missing_file_and_unsupported_language(tmp_path: Path) -> None:
    assert main(["run", str(GOLDEN / "fizzbuzz.yaml"), str(tmp_path / "nope.py")]) == 2
    t = tmp_path / "t.yaml"
    t.write_text("id: a\nlanguage: rust\nprompt: p\ntests: t\n")
    s = tmp_path / "s.py"
    s.write_text("x")
    assert main(["run", str(t), str(s)]) == 2


def test_secret_in_solution_not_logged(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    s = tmp_path / "s.txt"
    s.write_text(
        'API_KEY = "sk-hunter2-secret"\nraise SystemExit("sk-hunter2-secret")\n'
    )  # also fails the tests
    main(["run", str(GOLDEN / "fizzbuzz.yaml"), str(s), "--out", str(tmp_path / "r.json")])
    out = capfd.readouterr()
    assert "sk-hunter2-secret" not in out.out + out.err
