"""Known-malicious solutions must be contained (plan §18). Needs Docker; skipped without it."""

import shutil
import subprocess

import pytest

from evalry.runner.sandbox import run_python_tests

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
