"""Deterministic graders (plan §4). Each returns a GraderScore with the version needed to reproduce it."""

from typing import Literal

from evalry.runner.sandbox import run_python_tests, run_python_tests_rpc
from evalry.schemas import GraderScore, Task

# "2": hidden tests run out of process by default; a test that cannot be proxied fails closed (score 0).
PYTHON_TESTS_VERSION = "2"

Isolation = Literal["rpc", "inprocess"]
DEFAULT_ISOLATION: Isolation = "rpc"


def python_tests(
    task: Task, code: str, *, timeout_s: float = 10, isolation: Isolation = DEFAULT_ISOLATION
) -> tuple[GraderScore, bool]:
    """Score 1 if the hidden tests exit 0, else 0. The bool is the hard-fail flag (timeout) for ``composite``."""
    run = run_python_tests_rpc if isolation == "rpc" else run_python_tests
    r = run(code, task.tests, timeout_s=timeout_s)
    evidence = "timeout" if r.timed_out else r.stderr[-500:]
    score = GraderScore(
        grader="tests", grader_version=PYTHON_TESTS_VERSION, score=float(r.exit_code == 0), evidence=evidence
    )
    return score, r.timed_out
