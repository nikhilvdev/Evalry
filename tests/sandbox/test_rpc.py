"""Out-of-process test driver: hidden tests live in a different container from the solution. Needs Docker."""

import shutil
import subprocess
from pathlib import Path

import pytest

from evalry.graders.python_tests import python_tests
from evalry.runner.sandbox import run_python_tests, run_python_tests_rpc
from evalry.schemas import Task, load_task

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "info"], check=False, capture_output=True).returncode != 0,
    reason="Docker is not available",
)

GOLDEN = Path(__file__).parent.parent / "golden"


def no_containers_left() -> bool:
    out = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=evalry-"], check=False, capture_output=True, text=True
    )
    return out.stdout.strip() == ""


# The needle is assembled at run time so solution.py itself never contains it; it exists only in the tests. The scanner returns
# what it found and the (hidden) tests assert the list is empty, so the check happens on the side that knows the secret.
SCAN_SOLUTION = """
import gc, glob, sys

def scan():
    N = 'HIDDEN_MARK' + 'ER_9431'  # local only: a module-level needle would sit in the solution's own globals
    hits = []
    for pat in ('/proc/*/cmdline', '/proc/*/environ', '/work/*', '/tmp/*'):
        for path in glob.glob(pat):
            try:
                if N in open(path, 'rb').read().decode('latin1'):
                    hits.append(path)
            except Exception:
                pass
    hits += ['gc:' + o[:30] for o in gc.get_objects() if type(o) is str and N in o and o is not N]
    for frame in sys._current_frames().values():
        while frame:
            for name, value in list(frame.f_locals.items()):
                if value is not N and N in repr(value):
                    hits.append('frame:%s.%s' % (frame.f_code.co_name, name))
            frame = frame.f_back
    return hits

AT_IMPORT = scan()

def f():
    return [AT_IMPORT, scan()]
"""
SCAN_TESTS = "SECRET = 'HIDDEN_MARKER_9431_EXPECTED_VALUE'\nassert f() == [[], []]\n"


def test_solution_scanning_everything_never_finds_the_tests() -> None:
    r = run_python_tests_rpc(SCAN_SOLUTION, SCAN_TESTS)
    assert r.exit_code == 0, r.stderr


def test_same_scan_does_find_the_tests_in_process_which_is_why_rpc_exists() -> None:
    assert run_python_tests(SCAN_SOLUTION, SCAN_TESTS).exit_code != 0  # the old gap, kept as the contrast


@pytest.mark.parametrize("name", ["good", "wrong", "empty"])
def test_golden_scores_match_in_both_modes(name: str) -> None:
    task = load_task(GOLDEN / "fizzbuzz.yaml")
    code = (GOLDEN / "solutions" / f"{name}.txt").read_text()
    assert (
        python_tests(task, code, isolation="rpc")[0].score
        == python_tests(task, code, isolation="inprocess")[0].score
    )


def test_data_types_exceptions_kwargs_and_constants() -> None:
    code = (
        "LIMIT = 3\n"
        "def pair(a, b=2): return (a, b)\n"
        "def uniq(xs): return set(xs)\n"
        "def raw(): return b'ab'\n"
        "def boom(): raise ValueError('nope')\n"
        "def nan(): return float('nan')\n"
    )
    tests = (
        "assert LIMIT == 3\n"
        "assert pair(1) == (1, 2) and pair(1, b=5) == (1, 5)\n"
        "assert uniq([1, 1, 2]) == {1, 2} and raw() == b'ab'\n"
        "import math; assert math.isnan(nan())\n"
        "try:\n    boom()\nexcept ValueError as e:\n    assert str(e) == 'nope'\nelse:\n    raise AssertionError('no raise')\n"
    )
    r = run_python_tests_rpc(code, tests)
    assert r.exit_code == 0, r.stderr


def test_failed_assertion_and_import_error_and_missing_name() -> None:
    assert run_python_tests_rpc("def f(): return 2", "assert f() == 1").exit_code == 1
    r = run_python_tests_rpc("raise RuntimeError('boom')", "assert True")
    assert r.exit_code == 1 and "failed to import" in r.stderr
    assert run_python_tests_rpc("", "g()").exit_code == 1  # NameError in the tests


def test_unsupported_constructs_fail_closed_even_if_swallowed() -> None:
    cls = "class A:\n    pass\ndef make(): return A()\n"
    r = run_python_tests_rpc(cls, "A()")
    assert r.exit_code == 4 and "cannot cross" in r.stderr
    swallowed = run_python_tests_rpc(cls, "try:\n    make()\nexcept Exception:\n    pass\n")
    assert swallowed.exit_code == 4  # returned an object that cannot be proxied; swallowing it must not pass
    assert run_python_tests_rpc("def f(x): return 1", "f(lambda: 1)").exit_code == 4


def test_solution_that_dies_or_garbles_the_channel_fails_without_hanging() -> None:
    assert (
        run_python_tests_rpc("def f():\n    import os\n    os._exit(7)", "f()", timeout_s=20).exit_code == 5
    )
    garbage = "def f():\n    import os\n    os.write(1, b'not json\\n')\n    import time\n    time.sleep(30)"
    assert run_python_tests_rpc(garbage, "f()", timeout_s=20).exit_code == 5
    bomb = "def f():\n    import os\n    os.write(1, b'{\"ok\":' + b'[' * 200000 + b'\\n')\n    import time\n    time.sleep(30)"
    assert run_python_tests_rpc(bomb, "f()", timeout_s=20).exit_code == 5
    assert no_containers_left()


def test_oversized_message_is_a_relay_violation_and_kills_both_containers() -> None:
    flood = "def f():\n    import os\n    os.write(1, b'x' * (3 << 20) + b'\\n')"
    r = run_python_tests_rpc(flood, "f()", timeout_s=20)
    assert r.exit_code == -2 and "relay violation" in r.stderr
    assert no_containers_left()


def test_hang_in_solution_times_out_and_cleans_up() -> None:
    r = run_python_tests_rpc("def f():\n    while True: pass", "f()", timeout_s=3)
    assert r.timed_out and no_containers_left()


def test_fork_bomb_in_solution_is_contained() -> None:
    r = run_python_tests_rpc("def f():\n    import os\n    while True: os.fork()", "f()", timeout_s=15)
    assert r.timed_out or r.exit_code != 0
    assert no_containers_left()


def test_network_and_writes_still_blocked_in_both_containers() -> None:
    net = "import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=3)"
    assert run_python_tests_rpc(net, "assert True").exit_code != 0
    assert run_python_tests_rpc("open('/etc/evil', 'w').write('x')", "assert True").exit_code != 0
    t = Task(id="t", language="python", prompt="p", tests="open('/etc/evil', 'w')")
    assert python_tests(t, "def f(): pass")[0].score == 0.0  # tests container is read-only too
