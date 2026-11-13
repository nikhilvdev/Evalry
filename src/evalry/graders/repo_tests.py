"""Grader for repository tasks (plan §4): the repo's own test command, with the task's hidden tests copied over the edits."""

import json

from evalry.repo_tasks import Edits, Files, RepoError, build_workspace
from evalry.runner.repo import run_repo
from evalry.schemas import GraderScore, Task

REPO_TESTS_VERSION = "1"


def repo_tests(
    task: Task, snapshot: Files, edits: Edits, *, timeout_s: float = 300
) -> tuple[GraderScore, bool]:
    """Score 1 if the test command exits 0 on snapshot + edits + hidden tests. The bool is the hard-fail flag (timeout).

    Hidden tests are applied last, so an edit cannot replace them. A workspace that breaks a safety rule scores 0 with the reason.
    """
    assert task.repo, "repo_tests needs a tier 3 or 4 task"
    try:
        workspace = build_workspace(snapshot, edits, json.loads(task.tests))
    except RepoError as e:
        return GraderScore(
            grader="tests", grader_version=REPO_TESTS_VERSION, score=0.0, evidence=f"rejected: {e}"[:500]
        ), False
    r = run_repo(workspace, task.repo.test_command, task.repo.image, timeout_s=timeout_s)
    evidence = "timeout" if r.timed_out else (r.stdout + r.stderr)[-500:]
    return GraderScore(
        grader="tests", grader_version=REPO_TESTS_VERSION, score=float(r.exit_code == 0), evidence=evidence
    ), r.timed_out
