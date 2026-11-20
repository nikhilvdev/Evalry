import subprocess
import sys
import time
import tracemalloc
from typing import Any

import pytest

from evalry.runner import sandbox
from evalry.runner.sandbox import container_labels, reap_orphans, run_bounded

PY = sys.executable


def test_output_is_cut_to_the_tail_and_the_exit_code_is_kept() -> None:
    code, out, err, timed_out = run_bounded(
        [PY, "-c", "import sys; print('a' * 5000); print('b' * 5000); sys.stderr.write('E' * 3000 + 'END'); sys.exit(3)"],
        stdin=None, timeout_s=20, limit=100,
    )  # fmt: skip
    assert (code, timed_out) == (3, False)
    assert (
        len(out) == 100 and out.endswith(b"b\n") and set(out.strip()) == {ord("b")}
    )  # the end survives, not the start
    assert err.endswith(b"END") and len(err) == 100


def test_a_flood_of_output_costs_a_fixed_amount_of_host_memory() -> None:
    flood = "import sys\nchunk = b'x' * 65536\nwhile True:\n    sys.stdout.buffer.write(chunk)\n    sys.stderr.buffer.write(chunk)"
    tracemalloc.start()
    start = time.monotonic()
    code, out, err, timed_out = run_bounded([PY, "-c", flood], stdin=None, timeout_s=2)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert timed_out and code == -1 and time.monotonic() - start < 15
    assert len(out) <= 10_000 and len(err) <= 10_000  # the default limit
    assert peak < 20 * 1024 * 1024  # two seconds of flooding would be hundreds of MB if it were buffered


def test_a_timeout_kills_the_process_and_names_the_container_to_remove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "_kill_and_wait", lambda *names, **kw: killed.extend(names))
    code, _out, _err, timed_out = run_bounded(
        [PY, "-c", "import time; time.sleep(60)"], stdin=None, timeout_s=1, container="evalry-x"
    )
    assert (code, timed_out, killed) == (-1, True, ["evalry-x"])


def test_large_stdin_is_delivered_without_deadlock_even_when_the_child_floods_while_reading() -> None:
    echo = "import sys\ndata = sys.stdin.buffer.read()\nsys.stdout.buffer.write(data)\nsys.exit(0 if len(data) == 30_000_000 else 9)"
    code, out, _, timed_out = run_bounded([PY, "-c", echo], stdin=b"y" * 30_000_000, timeout_s=60, limit=1000)
    assert (code, timed_out, len(out)) == (0, False, 1000)


def test_a_child_that_exits_before_reading_its_stdin_is_not_an_error_in_the_runner() -> None:
    code, _, _, timed_out = run_bounded(
        [PY, "-c", "import sys; sys.exit(7)"], stdin=b"z" * 5_000_000, timeout_s=20
    )
    assert (code, timed_out) == (7, False)


# -- orphan reaping: deadline labels and the reaper's decision --------------------------------------------------------------


def test_every_container_is_labelled_with_its_own_deadline() -> None:
    before = int(time.time())
    labels = container_labels(100)
    assert labels[:2] == ["--label", "evalry.runner=1"] and labels[2] == "--label"
    assert (
        labels[labels.index("--log-driver") + 1] == "none"
    )  # a flooding container must not fill the daemon's disk with logs
    assert before + 100 <= int(labels[3].removeprefix("evalry.deadline=")) <= int(time.time()) + 100


def fake_docker(monkeypatch: pytest.MonkeyPatch, listing: str) -> list[str]:
    killed: list[str] = []
    monkeypatch.setattr(sandbox, "_kill_and_wait", lambda *names, **kw: killed.extend(names))
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=listing, stderr=""),
    )
    return killed


def test_the_reaper_removes_only_containers_past_their_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = "evalry-r-old\t1000\nevalry-a-live\t5000\nevalry-s-nolabel\t\nevalry-t-junk\tnot-a-number\n"
    killed = fake_docker(monkeypatch, listing)
    assert (
        reap_orphans(now=2000) == ["evalry-r-old"] == killed
    )  # live job untouched, unreadable deadlines not judged
    killed.clear()
    assert reap_orphans(now=9999) == ["evalry-r-old", "evalry-a-live"] == killed
    killed.clear()
    assert reap_orphans(now=10) == [] and killed == []


def test_the_reaper_never_crashes_a_worker_when_docker_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", boom)
    assert reap_orphans() == []
