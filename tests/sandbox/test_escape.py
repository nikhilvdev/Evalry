"""Known-malicious solutions must be contained (plan §18). Needs Docker; skipped without it."""

import shutil
import subprocess

import pytest

from evalry.runner.sandbox import SandboxResult, run_python_tests

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "info"], check=False, capture_output=True).returncode != 0,
    reason="Docker is not available",
)

PASS = "assert True"


def test_passing_solution() -> None:
    assert run_python_tests("def f(): return 1", "assert f() == 1").exit_code == 0


def test_failing_assertion() -> None:
    assert run_python_tests("def f(): return 2", "assert f() == 1").exit_code != 0


def test_network_blocked() -> None:
    code = "import socket\ns = socket.create_connection(('1.1.1.1', 53), timeout=3)"
    assert run_python_tests(code, PASS).exit_code != 0


def test_write_outside_workspace_blocked() -> None:
    assert run_python_tests("open('/etc/evil', 'w').write('x')", PASS).exit_code != 0
    assert run_python_tests("open('/usr/evil', 'w').write('x')", PASS).exit_code != 0


def test_secrets_not_in_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALRY_FAKE_KEY", "sk-should-not-leak")
    code = "import os\nassert 'EVALRY_FAKE_KEY' not in os.environ, dict(os.environ)"
    assert run_python_tests(code, PASS).exit_code == 0


def test_runs_as_non_root() -> None:
    assert run_python_tests("import os\nassert os.getuid() != 0", PASS).exit_code == 0


def test_fork_bomb_contained() -> None:
    code = "import os\nwhile True:\n    os.fork()"
    r = run_python_tests(code, PASS, timeout_s=15)
    assert r.timed_out or r.exit_code != 0  # contained by the pids limit; host stays responsive


def test_infinite_loop_times_out_and_container_is_removed() -> None:
    r = run_python_tests("while True: pass", PASS, timeout_s=3)
    assert r.timed_out
    left = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=evalry-"], check=False, capture_output=True, text=True
    )
    assert left.stdout.strip() == ""


def test_solution_cannot_read_hidden_tests_at_import() -> None:
    # Module-level code runs before the tests exist in the container: not on disk, not yet on stdin.
    code = (
        "import glob, select, sys\n"
        "marker = 'HIDDEN_MARK' + 'ER_9431'\n"  # built at runtime so solution.py itself never contains it
        "for f in glob.glob('/work/*'):\n"
        "    assert marker not in open(f).read(), f\n"
        "assert not select.select([sys.stdin], [], [], 0.2)[0], 'tests already on stdin'\n"
        "def f(): return 1\n"
    )
    r = run_python_tests(code, "# HIDDEN_MARKER_9431\nassert f() == 1")
    assert r.exit_code == 0, r.stderr


def test_failing_import_fails_the_run() -> None:
    assert run_python_tests("raise RuntimeError('boom')", PASS).exit_code != 0


def test_hash_seed_is_pinned() -> None:
    assert run_python_tests("import os\nassert os.environ['PYTHONHASHSEED'] == '0'", PASS).exit_code == 0


# -- repository and agent sandboxes (tiers 3 and 4) ----------------------------------------------------------------------

import json
from typing import Any

from evalry.agent.loop import AgentTurn
from evalry.generation.base import FakeGenerator
from evalry.repo_tasks import build_workspace
from evalry.runner.repo import AgentContainer, run_repo
from evalry.runner.sandbox import PYTHON_IMAGE
from evalry.scheduler.engine import run_sample
from evalry.schemas import Task

BUGGY = {"app.py": b"def add(a, b):\n    return a - b\n", "README.md": b"demo"}
HIDDEN = {
    "tests/test_hidden.py": "import unittest\nimport app\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(app.add(1, 2), 3)\n"
}
REPO_SPEC = {
    "snapshot": "c" * 64,
    "commit": "abc1234",
    "image": PYTHON_IMAGE,
    "test_command": ["python", "-m", "unittest", "tests.test_hidden"],
}


def repo_cmd(code: str) -> SandboxResult:
    return run_repo(build_workspace(BUGGY), ["python", "-c", code], PYTHON_IMAGE, timeout_s=30)


def leftovers() -> str:
    return subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=evalry-"], check=False, capture_output=True, text=True
    ).stdout.strip()


def test_repo_runner_runs_the_repos_own_tests_and_reports_the_verdict() -> None:
    good = {"app.py": "def add(a, b):\n    return a + b\n"}
    ok = run_repo(build_workspace(BUGGY, good, HIDDEN), REPO_SPEC["test_command"], PYTHON_IMAGE, timeout_s=60)
    bad = run_repo(build_workspace(BUGGY, HIDDEN), REPO_SPEC["test_command"], PYTHON_IMAGE, timeout_s=60)
    assert ok.exit_code == 0 and bad.exit_code != 0 and not (ok.timed_out or bad.timed_out)


def test_repo_runner_blocks_network_outside_writes_and_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALRY_FAKE_KEY", "sk-should-not-leak")
    assert repo_cmd("import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)").exit_code != 0
    assert repo_cmd("open('/etc/evil', 'w').write('x')").exit_code != 0
    assert repo_cmd("open('/usr/evil', 'w').write('x')").exit_code != 0
    assert repo_cmd("import os; assert 'EVALRY_FAKE_KEY' not in os.environ, dict(os.environ)").exit_code == 0
    assert repo_cmd("import os; assert os.getuid() != 0").exit_code == 0
    assert (
        repo_cmd(
            "import os; assert os.path.exists('/work/repo/app.py') and not os.path.exists('/var/run/docker.sock')"
        ).exit_code
        == 0
    )
    assert repo_cmd("open('/work/ok.txt', 'w').write('x')").exit_code == 0  # the workspace itself is writable


def test_repo_runner_contains_a_fork_bomb_and_a_hang_and_leaves_nothing_behind() -> None:
    r = run_repo(
        build_workspace(BUGGY),
        ["python", "-c", "import os\nwhile True:\n    os.fork()"],
        PYTHON_IMAGE,
        timeout_s=20,
    )
    assert r.timed_out or r.exit_code != 0
    hang = run_repo(build_workspace(BUGGY), ["python", "-c", "while True: pass"], PYTHON_IMAGE, timeout_s=3)
    assert hang.timed_out and leftovers() == ""


def test_hostile_edits_cannot_name_a_path_outside_the_repo() -> None:
    from evalry.repo_tasks import RepoError

    for path in ("../../etc/cron.d/x", "/etc/passwd", ".git/hooks/pre-commit"):
        with pytest.raises(RepoError):
            build_workspace(BUGGY, {path: "x"})


def test_agent_container_keeps_state_between_commands_is_contained_and_cleans_up() -> None:
    with AgentContainer(build_workspace(BUGGY), PYTHON_IMAGE) as ws:
        assert ws.exec(["sh", "-c", "echo hi > made.txt"]).exit_code == 0
        assert ws.exec(["cat", "made.txt"]).stdout.strip() == "hi"  # state persists across execs
        assert (
            ws.exec(
                ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"]
            ).exit_code
            != 0
        )
        assert ws.exec(["sh", "-c", "echo x > /etc/evil"]).exit_code != 0
        assert ws.exec(["id", "-u"]).stdout.strip() != "0"
        assert b"made.txt" in ws.export()
    assert leftovers() == ""


def test_agent_command_timeout_kills_the_whole_container() -> None:
    with AgentContainer(build_workspace(BUGGY), PYTHON_IMAGE) as ws:
        r = ws.exec(["sleep", "60"], timeout_s=2)
        assert r.timed_out and ws.dead
        assert ws.exec(
            ["echo", "still there?"]
        ).timed_out  # nothing runs in a container that may hold a stray process
    assert leftovers() == ""


class ScriptedAgent:
    """Fixes the bug through the real tools, runs the tests, and finishes."""

    def __init__(self) -> None:
        self.n = 0

    def step(self, system: str, messages: list[dict[str, Any]]) -> AgentTurn:
        self.n += 1
        write = {
            "type": "tool_use",
            "id": "w",
            "name": "write_file",
            "input": {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"},
        }
        run = {
            "type": "tool_use",
            "id": "b",
            "name": "bash",
            "input": {"command": "python -c 'import app; assert app.add(1, 2) == 3' && echo unittest ok"},
        }
        content = [[write], [run], [{"type": "text", "text": "fixed"}]][min(self.n, 3) - 1]
        return AgentTurn(
            content=content,
            stop_reason="tool_use" if self.n < 3 else "end_turn",
            model="scripted",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
        )


def task(tier: int) -> Task:
    extra: dict[str, Any] = {"agent": {"max_steps": 6}} if tier == 4 else {}
    return Task(
        id="r",
        language="python",
        tier=tier,
        prompt="add() subtracts",
        tests=json.dumps(HIDDEN),
        repo=REPO_SPEC,
        **extra,
    )  # type: ignore[arg-type]


def test_tier_4_end_to_end_agent_edits_in_a_sandbox_and_hidden_tests_grade_it_in_another() -> None:
    r = run_sample(task(4), FakeGenerator([]), 0, snapshot=BUGGY, agent=ScriptedAgent())
    assert r.composite == 1.0 and r.isolation == "inprocess" and r.generator_model == "scripted"
    assert r.agent and r.agent.finished and r.agent.tool_calls == {"write_file": 1, "bash": 1}
    assert "tests_failed" not in r.agent.failure_categories and 'path="app.py"' in r.code
    assert (
        "test_hidden" not in r.code and leftovers() == ""
    )  # the hidden tests never entered the agent's workspace or its diff
    lazy = run_sample(
        task(4),
        FakeGenerator([]),
        0,
        snapshot=BUGGY,
        agent=type(
            "Lazy",
            (),
            {
                "step": lambda s, sy, m: AgentTurn(
                    content=[{"type": "text", "text": "no"}], stop_reason="end_turn", model="m"
                )
            },
        )(),
    )  # type: ignore[arg-type]
    assert lazy.composite == 0.0 and {"tests_failed", "never_ran_tests"} <= set(lazy.agent.failure_categories)  # type: ignore[union-attr]


def test_tier_3_end_to_end_model_edits_then_the_repos_tests_decide() -> None:
    fixed = FakeGenerator(['<file path="app.py">\ndef add(a, b):\n    return a + b\n</file>'])
    r = run_sample(task(3), fixed, 0, snapshot=BUGGY)
    assert r.composite == 1.0 and r.agent is None and "test_hidden" not in fixed.prompts[0]
    assert run_sample(task(3), FakeGenerator(["I could not fix it"]), 0, snapshot=BUGGY).composite == 0.0
    cheat = FakeGenerator(
        [
            '<file path="tests/test_hidden.py">\nimport unittest\nclass T(unittest.TestCase):\n    def test_add(self):\n        pass\n</file>'
        ]
    )
    assert (
        run_sample(task(3), cheat, 0, snapshot=BUGGY).composite == 0.0
    )  # replacing the hidden tests does not help: they are applied last
