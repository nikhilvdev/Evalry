from evalry.graders import python_tests
from evalry.schemas import Task

T = Task(id="t", language="python", prompt="p", tests="assert f() == 1")


def test_python_tests_scores_pass_fail_and_timeout() -> None:
    assert python_tests(T, "def f(): return 1")[0].score == 1.0
    s, hard = python_tests(T, "def f(): return 2")
    assert (s.score, hard) == (0.0, False) and "AssertionError" in s.evidence
    s, hard = python_tests(T, "", timeout_s=3)  # missing f -> NameError, fast failure
    assert s.score == 0.0
    s, hard = python_tests(T, "while True: pass", timeout_s=3)
    assert (s.score, hard, s.evidence) == (0.0, True, "timeout")


def test_empty_output_scores_zero() -> None:
    assert python_tests(T, "")[0].score == 0.0
