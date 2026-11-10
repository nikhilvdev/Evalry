"""Deterministic graders (plan §4). Each returns a GraderScore with the version needed to reproduce it."""

from evalry.runner import run_python_tests
from evalry.schemas import GraderScore, Task

PYTHON_TESTS_VERSION = "1"


def python_tests(task: Task, code: str, *, timeout_s: float = 10) -> tuple[GraderScore, bool]:
    """Score 1 if the hidden tests exit 0, else 0. The bool is the hard-fail flag (timeout) for ``composite``."""
    r = run_python_tests(code, task.tests, timeout_s=timeout_s)
    evidence = "timeout" if r.timed_out else r.stderr[-500:]
    score = GraderScore(
        grader="tests", grader_version=PYTHON_TESTS_VERSION, score=float(r.exit_code == 0), evidence=evidence
    )
    return score, r.timed_out
