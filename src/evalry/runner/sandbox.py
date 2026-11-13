"""Docker sandbox for untrusted code (plan §7, §9). Model-generated code never runs outside this module."""

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import IO, NamedTuple

from evalry.log import get_logger

# Pinned by digest so a moved tag cannot change scores. shortcut: Python 3.12 only, a language profile registry comes later.
PYTHON_IMAGE = "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
_MAX_OUTPUT = 10_000
SEED = 0  # fixed PYTHONHASHSEED so set/dict ordering cannot flip a score; recorded on every sample

# Runs inside the container. The solution is imported in a child process first; the hidden tests are sent over a pipe only
# after the import finished, and never written to disk. Module-level solution code therefore cannot read them from the
# workspace or stdin. shortcut: code that survives import (threads, frame inspection) can still reach the tests in-process;
# closing that needs an out-of-process test driver (phase 4 isolation work).
_BOOT = """
import json, os, subprocess, sys
p = json.load(sys.stdin)
open('/work/solution.py', 'w').write(p['code'])
r, w = os.pipe()
child = subprocess.Popen([sys.executable, '-c', CHILD, str(w)], cwd='/work', stdin=subprocess.PIPE, pass_fds=[w])
os.close(w)
if os.read(r, 5) != b'READY':
    sys.exit(child.wait() or 1)
child.stdin.write(p['tests'].encode())
child.stdin.close()
sys.exit(child.wait())
"""
_CHILD = """
import os, sys
sys.path.insert(0, '/work')
import solution
os.write(int(sys.argv[1]), b'READY')
os.close(int(sys.argv[1]))
ns = dict(vars(solution))
exec(compile(sys.stdin.read(), 'tests', 'exec'), ns)
"""

log = get_logger("runner")


class SandboxResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _docker_cmd(name: str, image: str, source: str) -> list[str]:
    """One hardened container per process: no network, non-root, read-only root, capped resources, no shared volumes."""
    return [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none", "--read-only", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
        "--tmpfs", "/work:rw,nosuid,size=16m,mode=1777",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"PYTHONHASHSEED={SEED}",
        image, "python", "-c", source,
    ]  # fmt: skip


def _kill_and_wait(*names: str, wait_s: float = 10) -> None:
    """Kill containers and wait until Docker has really removed them, so "cleaned up" is true when the caller resumes.

    ``docker kill`` returns before ``--rm`` has finished removing the container; callers that check for leftovers (or start
    the next job against a resource limit) would otherwise race with the removal.
    """
    for n in names:
        subprocess.run(["docker", "kill", n], check=False, capture_output=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        left = [
            n
            for n in names
            if subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=^{n}$"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ]
        if not left:
            return
        time.sleep(0.05)
    log.warn("container still present after kill", containers=",".join(names))  # noqa: G010 (LogQuill API)


def run_python_tests(
    code: str, tests: str, *, timeout_s: float = 10, image: str = PYTHON_IMAGE
) -> SandboxResult:
    """Run ``tests`` against ``code`` in a throwaway container: no network, non-root, read-only root fs, capped resources."""
    name = f"evalry-{uuid.uuid4().hex[:12]}"
    cmd = _docker_cmd(name, image, "CHILD = " + repr(_CHILD) + _BOOT)
    payload = json.dumps({"code": code, "tests": tests})
    try:
        done = subprocess.run(
            cmd, input=payload, check=False, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        # The docker client dying does not stop the container, so kill it by name.
        _kill_and_wait(name)
        log.warn("sandbox timeout", container=name, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    return SandboxResult(done.returncode, done.stdout[:_MAX_OUTPUT], done.stderr[:_MAX_OUTPUT], False)


# --- Out-of-process test driver (closes threat T5): solution and tests run in separate containers ---------------------

_MAX_LINE = 1 << 20  # one relayed message; a larger line kills the run
_PKG = Path(__file__).parent
# Registers evalry.runner._codec inside the container (evalry itself is not installed there), then runs a driver's main().
_BOOTSTRAP = (
    "import sys, types\n"
    "_p = types.ModuleType('evalry'); _p.__path__ = []\n"
    "_r = types.ModuleType('evalry.runner'); _r.__path__ = []\n"
    "_c = types.ModuleType('evalry.runner._codec'); exec(CODEC, _c.__dict__)\n"
    "_p.runner = _r; _r._codec = _c\n"
    "sys.modules.update({'evalry': _p, 'evalry.runner': _r, 'evalry.runner._codec': _c})\n"
)


def _driver_source(driver: str) -> str:
    codec = (_PKG / "_codec.py").read_text()
    return f"CODEC = {codec!r}\n{_BOOTSTRAP}{(_PKG / driver).read_text()}\nmain()\n"


def _relay(src: IO[bytes], dst: IO[bytes], violation: list[str], stop: threading.Event, label: str) -> None:
    """Copy JSON lines src -> dst without looking inside them. Never raises; closes dst when src ends."""
    try:
        while line := src.readline(_MAX_LINE + 1):
            if len(line) > _MAX_LINE or not line.endswith(b"\n"):
                violation.append(f"{label} sent a message over {_MAX_LINE} bytes")
                stop.set()
                break
            dst.write(line)
            dst.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            dst.close()
        except (OSError, ValueError):
            pass


def _drain(src: IO[bytes], sink: list[bytes]) -> None:
    """Keep the first bytes of a stream and discard the rest, so a chatty process can never block on a full pipe."""
    kept = 0
    for chunk in iter(lambda: src.read(4096), b""):
        if kept < _MAX_OUTPUT:
            sink.append(chunk[: _MAX_OUTPUT - kept])
            kept += len(chunk)


def run_python_tests_rpc(
    code: str, tests: str, *, timeout_s: float = 10, image: str = PYTHON_IMAGE
) -> SandboxResult:
    """Run the solution (container S) and the hidden tests (container T) apart, joined only by a byte relay on the host.

    The tests never exist in any process or filesystem the solution can reach. Exit code is T's verdict; see _driver_t.
    """
    uid = uuid.uuid4().hex[:12]
    names = {"s": f"evalry-s-{uid}", "t": f"evalry-t-{uid}"}
    procs = {
        k: subprocess.Popen(
            _docker_cmd(names[k], image, _driver_source(f"_driver_{k}.py")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for k in ("s", "t")
    }  # fmt: skip
    s, t = procs["s"], procs["t"]
    assert (
        s.stdin and s.stdout and s.stderr and t.stdin and t.stdout and t.stderr
    )  # PIPE was requested for all
    violation: list[str] = []
    stop = threading.Event()  # set by a relay on a violation so the run ends at once, not at the timeout
    errs: dict[str, list[bytes]] = {"s": [], "t": []}
    try:
        s.stdin.write(json.dumps({"code": code}).encode() + b"\n")
        s.stdin.flush()
        t.stdin.write(json.dumps({"tests": tests}).encode() + b"\n")
        t.stdin.flush()
    except OSError:
        pass  # a container that died at start is reported through its exit code below
    threads = [
        threading.Thread(target=_relay, args=(s.stdout, t.stdin, violation, stop, "solution"), daemon=True),
        threading.Thread(target=_relay, args=(t.stdout, s.stdin, violation, stop, "tests"), daemon=True),
        threading.Thread(target=_drain, args=(s.stderr, errs["s"]), daemon=True),
        threading.Thread(target=_drain, args=(t.stderr, errs["t"]), daemon=True),
    ]
    for th in threads:
        th.start()
    deadline = time.monotonic() + timeout_s
    while t.poll() is None and not stop.is_set() and time.monotonic() < deadline:
        stop.wait(0.02)
    timed_out = t.poll() is None and not violation
    # The run is over: never leave either container behind.
    still_running = [names[k] for k in ("s", "t") if procs[k].poll() is None]
    if still_running:
        _kill_and_wait(*still_running)
    for p in procs.values():
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()  # the container is already gone; a client blocked writing into a pipe nobody reads must not hang us
            p.wait()
    for th in threads:
        th.join(timeout=2)
    if timed_out:
        log.warn("sandbox timeout", container=uid, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    err = b"".join(errs["s"]).decode(errors="replace")[:2000] + b"".join(errs["t"]).decode(errors="replace")
    if violation:
        log.warn("relay violation", container=uid, reason=violation[0])  # noqa: G010 (LogQuill API)
        return SandboxResult(-2, "", f"relay violation: {violation[0]}\n{err}"[:_MAX_OUTPUT], False)
    return SandboxResult(t.returncode, "", err[-_MAX_OUTPUT:], False)
