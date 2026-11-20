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
from typing import Any, Literal

from evalry.agent.loop import AgentTurn
from evalry.generation.base import FakeGenerator
from evalry.repo_tasks import build_workspace
from evalry.runner.repo import AgentContainer, run_repo
from evalry.runner.sandbox import PYTHON_IMAGE
from evalry.scheduler.engine import run_sample
from evalry.schemas import RepoSpec, Task

BUGGY = {"app.py": b"def add(a, b):\n    return a - b\n", "README.md": b"demo"}
HIDDEN = {
    "tests/test_hidden.py": "import unittest\nimport app\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(app.add(1, 2), 3)\n"
}
REPO_SPEC: dict[str, Any] = {
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
        turns: list[list[dict[str, Any]]] = [[write], [run], [{"type": "text", "text": "fixed"}]]
        content = turns[min(self.n, 3) - 1]
        return AgentTurn(
            content=content,
            stop_reason="tool_use" if self.n < 3 else "end_turn",
            model="scripted",
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
        )


def task(tier: Literal[3, 4]) -> Task:
    extra: dict[str, Any] = {"agent": {"max_steps": 6}} if tier == 4 else {}
    return Task(
        id="r",
        language="python",
        tier=tier,
        prompt="add() subtracts",
        tests=json.dumps(HIDDEN),
        repo=RepoSpec.model_validate(REPO_SPEC),
        **extra,
    )


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
    )
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


# -- output flooding and orphaned containers (security review findings F12 and F13) -------------------------------------------

import signal
import time
import tracemalloc

from evalry.runner import repo as repo_runner
from evalry.runner.repo import repo_command
from evalry.runner.sandbox import reap_orphans

FLOOD = "import sys\nc = b'x' * 65536\nwhile True:\n    sys.stdout.buffer.write(c)\n    sys.stderr.buffer.write(c)"


def wait_until_gone(name: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=^{name}$"], check=False, capture_output=True, text=True
        ).stdout.strip():
            return True
        time.sleep(0.5)
    return False


def test_a_container_that_floods_its_output_cannot_exhaust_host_memory() -> None:
    tracemalloc.start()
    tier1 = run_python_tests(f"exec({FLOOD!r})", PASS, timeout_s=6)
    repo = run_repo(build_workspace(BUGGY), ["python", "-c", FLOOD], PYTHON_IMAGE, timeout_s=6)
    with AgentContainer(build_workspace(BUGGY), PYTHON_IMAGE, lifetime_s=120) as ws:
        agent = ws.exec(["python", "-c", FLOOD], timeout_s=6)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert tier1.timed_out and repo.timed_out and agent.timed_out
    assert peak < 50 * 1024 * 1024  # seconds of flooding per runner would be gigabytes if buffered
    assert leftovers() == ""


def test_the_output_that_is_kept_is_the_end_where_test_failures_are_reported() -> None:
    code = (
        "import sys\nprint('noise ' * 20000)\nsys.stderr.write('Traceback: the real failure\\n')\nsys.exit(1)"
    )
    r = run_repo(build_workspace(BUGGY), ["python", "-c", code], PYTHON_IMAGE, timeout_s=30)
    assert r.exit_code == 1 and "the real failure" in r.stderr and len(r.stdout) <= 10_000


def test_an_agent_container_ends_by_itself_when_its_lifetime_is_up() -> None:
    ws = AgentContainer(build_workspace(BUGGY), PYTHON_IMAGE, lifetime_s=4)
    ws.__enter__()  # deliberately never closed: this is a worker that died
    try:
        assert not wait_until_gone(ws.name, 1)  # still alive early on
        assert wait_until_gone(ws.name, 25), "the container outlived its lifetime"
    finally:
        ws.dead = False
        ws.close()


def test_a_repo_test_container_stops_itself_when_the_host_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo_runner, "WATCHDOG_SLACK_S", 1)
    name = "evalry-r-hostdeath"
    cmd = repo_command(
        name, ["python", "-c", "while True: pass"], PYTHON_IMAGE, timeout_s=2
    )  # watchdog fires at 3 s
    client = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    assert client.stdin
    client.stdin.write(build_workspace(BUGGY))
    client.stdin.close()
    time.sleep(1.5)
    assert subprocess.run(
        ["docker", "ps", "-q", "--filter", f"name=^{name}$"], check=False, capture_output=True, text=True
    ).stdout.strip()
    client.send_signal(signal.SIGKILL)  # the worker dies: nothing is left to run docker kill
    client.wait()
    try:
        assert wait_until_gone(name, 25), "the watchdog did not stop the container"
    finally:
        subprocess.run(["docker", "kill", name], check=False, capture_output=True)


def test_the_reaper_removes_a_real_expired_container_and_leaves_a_live_one() -> None:
    def start(name: str, deadline: float) -> None:
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "--label", "evalry.runner=1", "--label", f"evalry.deadline={int(deadline)}",
             "--network", "none", PYTHON_IMAGE, "sleep", "300"],
            check=True, capture_output=True,
        )  # fmt: skip

    start("evalry-orphan-old", time.time() - 60)
    start("evalry-orphan-live", time.time() + 600)
    try:
        assert reap_orphans() == ["evalry-orphan-old"]
        assert wait_until_gone("evalry-orphan-old", 10)
        assert not wait_until_gone(
            "evalry-orphan-live", 1
        )  # a job still inside its deadline is never touched
    finally:
        for n in ("evalry-orphan-old", "evalry-orphan-live"):
            subprocess.run(["docker", "kill", n], check=False, capture_output=True)
        wait_until_gone("evalry-orphan-live", 10)


def test_reap_command_and_worker_start_clean_up_orphans(capsys: pytest.CaptureFixture[str]) -> None:
    from evalry import cli

    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", "evalry-orphan-cli", "--label", "evalry.runner=1", "--label", "evalry.deadline=1",
         "--network", "none", PYTHON_IMAGE, "sleep", "300"],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        assert cli.main(["reap"]) == 0 and "evalry-orphan-cli" in capsys.readouterr().out
        assert wait_until_gone("evalry-orphan-cli", 10)
    finally:
        subprocess.run(["docker", "kill", "evalry-orphan-cli"], check=False, capture_output=True)


# -- the strict seccomp profile (security review F2) ------------------------------------------------------------------------

PTRACE = "import ctypes, os\nlibc = ctypes.CDLL(None, use_errno=True)\nr = libc.ptrace(0, 0, 0, 0)  # PTRACE_TRACEME\nopen('/work/r', 'w').write(str(r))\nassert r != 0, 'ptrace was allowed'"
NETLINK = "import socket\ns = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)"
INET = "import socket\ns = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\ns2 = socket.socketpair()"


def test_strict_profile_refuses_ptrace_and_netlink_and_docker_default_allows_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVALRY_SECCOMP_PROFILE", raising=False)
    assert run_python_tests(PTRACE, PASS).exit_code == 0  # blocked: the in-container assertion holds
    assert run_python_tests(NETLINK, PASS).exit_code != 0  # PermissionError creating the socket
    monkeypatch.setenv("EVALRY_SECCOMP_PROFILE", "default")
    assert (
        run_python_tests(PTRACE, PASS).exit_code != 0
    )  # the control: Docker's default lets the call through
    assert run_python_tests(NETLINK, PASS).exit_code == 0


def test_strict_profile_still_lets_ordinary_programs_run() -> None:
    assert (
        run_python_tests(INET, PASS).exit_code == 0
    )  # INET sockets and socketpair (used by subprocess, asyncio) still work
    assert (
        run_python_tests(
            "import subprocess, sys\nassert subprocess.run([sys.executable, '-c', 'print(1)'], capture_output=True).stdout == b'1\\n'",
            PASS,
        ).exit_code
        == 0
    )
    assert (
        run_python_tests(
            "import threading\nt = threading.Thread(target=lambda: None)\nt.start()\nt.join()", PASS
        ).exit_code
        == 0
    )
    r = run_repo(
        build_workspace(BUGGY, {"app.py": "def add(a, b):\n    return a + b\n"}, HIDDEN),
        REPO_SPEC["test_command"],
        PYTHON_IMAGE,
        timeout_s=60,
    )
    assert r.exit_code == 0  # a unittest run of a repo works under the profile


# -- the node check against a real node (security review F1) -------------------------------------------------------------------

from evalry.runner import nodecheck


def test_the_node_check_passes_on_this_node_with_no_failures() -> None:
    checks = nodecheck.run_node_checks()
    assert [c for c in checks if c.status == "fail"] == [], nodecheck.render(checks)
    assert {"C1", "C2", "C3", "C4", "C5", "C6", "C8", "C9", "C10", "R1", "R3"} <= {
        c.id for c in checks if c.status == "pass"
    }


def test_the_probe_does_detect_a_weak_container_negative_control() -> None:
    """The same probe in a container run as root, with capabilities, a writable root, a network and no seccomp must fail checks."""
    weak = subprocess.run(
        ["docker", "run", "--rm", "--user", "0:0", "--security-opt", "seccomp=unconfined", "--cap-add", "SYS_PTRACE",
         PYTHON_IMAGE, "python", "-c", nodecheck.PROBE],
        check=True, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    failed = {
        c.id
        for c in nodecheck.probe_checks(json.loads(weak.stdout.strip().splitlines()[-1]), {})
        if c.status == "fail"
    }
    assert {"C6", "C1", "C3", "C9", "C10", "R3", "R1"} <= failed, (
        failed
    )  # root, capabilities, writable root, no seccomp, no limits
    assert leftovers() == ""
