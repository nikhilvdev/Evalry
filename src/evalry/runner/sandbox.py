"""Docker sandbox for untrusted code (plan §7, §9). Model-generated code never runs outside this module."""

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, NamedTuple

from evalry.images import PYTHON_IMAGE
from evalry.log import get_logger

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

__all__ = ["PYTHON_IMAGE"]  # re-exported from evalry.images: callers and tests import it from here

log = get_logger("runner")


class SandboxResult(NamedTuple):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


RUNNER_LABEL = "evalry.runner"
# Docker's default seccomp profile (moby/profiles, fetched 2026-11-13) with the syscalls a no-network grading sandbox never needs
# removed: ptrace, process_vm_readv, process_vm_writev (reading another process's memory) and name_to_handle_at, and socket()
# limited to the UNIX, INET and INET6 families (no netlink, packet or vsock sockets). Everything else is Docker's default.
SECCOMP_PROFILE = Path(__file__).parent / "seccomp.json"
_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
)  # starts with a letter or digit, so it can never look like a docker option


def hardening_flags(env: Mapping[str, str] | None = None) -> list[str]:
    """Limits every sandbox container gets, plus the optional stronger isolation an operator chooses (security review F1, F2, F7).

    Always: open files capped, core dumps off, single file size capped. Optional, from the environment, failing closed (a
    setting that cannot be honoured is an error, never a silent fallback to weaker isolation):
    ``EVALRY_DOCKER_RUNTIME`` (for example ``runsc`` for gVisor), ``EVALRY_SECCOMP_PROFILE`` (a profile file; ``default`` uses
    Docker's own profile instead of the stricter one shipped here, which is on by default) and ``EVALRY_APPARMOR_PROFILE``
    (name of a loaded profile).
    """
    env = os.environ if env is None else env
    flags = ["--ulimit", "nofile=1024:1024", "--ulimit", "core=0", "--ulimit", "fsize=268435456"]
    if runtime := env.get("EVALRY_DOCKER_RUNTIME"):
        if not _NAME.fullmatch(runtime):
            raise ValueError("EVALRY_DOCKER_RUNTIME must be a runtime name such as runsc.")
        flags += ["--runtime", runtime]
    profile = env.get("EVALRY_SECCOMP_PROFILE") or str(SECCOMP_PROFILE)
    if profile != "default":  # "default" opts back into Docker's own profile
        if not Path(profile).is_file():
            raise ValueError(
                f"EVALRY_SECCOMP_PROFILE {profile!r} is not a file. Fix the path rather than run without it."
            )
        flags += ["--security-opt", f"seccomp={profile}"]
    if apparmor := env.get("EVALRY_APPARMOR_PROFILE"):
        if not _NAME.fullmatch(apparmor):
            raise ValueError("EVALRY_APPARMOR_PROFILE must be the name of a loaded AppArmor profile.")
        flags += ["--security-opt", f"apparmor={apparmor}"]
    return flags


_slots = threading.Condition()
_in_use = 0


def max_containers(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    raw = env.get("EVALRY_MAX_CONTAINERS", "16")
    if not raw.isdigit() or int(raw) < 2:
        raise ValueError(
            "EVALRY_MAX_CONTAINERS must be a number of at least 2 (a tier 1 rpc job runs two containers)."
        )
    return int(raw)


def acquire_slots(n: int = 1, *, wait_s: float = 600) -> None:
    """Wait until ``n`` more containers fit under the cap, taking them together so two jobs cannot each hold half of what they
    need (security review F11). The cap is per worker process: run one worker process per runner node, or divide the node's
    capacity between them. Raises RuntimeError after ``wait_s`` so the job is retried instead of piling up."""
    global _in_use
    cap = max_containers()
    deadline = time.monotonic() + wait_s
    with _slots:
        while _in_use + n > cap:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RuntimeError(
                    f"Runner is at its container limit ({cap}). The job will be retried; raise EVALRY_MAX_CONTAINERS if the node has room."
                )
            _slots.wait(left)
        _in_use += n


def release_slots(n: int = 1) -> None:
    global _in_use
    with _slots:
        _in_use = max(0, _in_use - n)
        _slots.notify_all()


@contextmanager
def container_slots(n: int = 1) -> Iterator[None]:
    acquire_slots(n)
    try:
        yield
    finally:
        release_slots(n)


def container_labels(deadline_s: float) -> list[str]:
    """Mark a container as ours and say when it must be gone, so ``reap_orphans`` can remove it if its worker died.

    Also turns the Docker log driver off: by default Docker stores everything a container prints on the host's disk with no
    size limit, so a container that floods its output would fill the daemon's disk even though we only keep the tail. We read
    output through the attached streams and never use ``docker logs``.
    """
    return [
        "--label", f"{RUNNER_LABEL}=1", "--label", f"evalry.deadline={int(time.time() + deadline_s)}",
        "--log-driver", "none", *hardening_flags(),
    ]  # fmt: skip


def _docker_cmd(name: str, image: str, source: str, *, deadline_s: float = 120) -> list[str]:
    """One hardened container per process: no network, non-root, read-only root, capped resources, no shared volumes."""
    return [
        "docker", "run", "--rm", "-i", "--name", name, *container_labels(deadline_s),
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


def _tail(src: IO[bytes], sink: bytearray, limit: int) -> None:
    """Read a stream to its end but keep only the last ``limit`` bytes, so a container that floods its output costs the host
    a fixed amount of memory (and never blocks on a full pipe). The end is what matters: test summaries and tracebacks."""
    try:
        while chunk := src.read1(65536):  # type: ignore[attr-defined]  # BufferedReader
            sink += chunk
            if len(sink) > limit:
                del sink[: len(sink) - limit]
    except (OSError, ValueError):
        pass


def _feed(dst: IO[bytes], data: bytes) -> None:
    try:
        dst.write(data)
    except (OSError, ValueError):
        pass  # the container exited before reading everything; its exit code tells the story
    finally:
        try:
            dst.close()
        except (OSError, ValueError):
            pass


def run_bounded(
    cmd: list[str],
    *,
    stdin: bytes | None,
    timeout_s: float,
    container: str | None = None,
    limit: int = _MAX_OUTPUT,
) -> tuple[int, bytes, bytes, bool]:
    """Run a docker client command with bounded host memory. Returns (exit code, stdout tail, stderr tail, timed out).

    Output never accumulates beyond ``limit`` bytes per stream. On timeout the container (when named) is killed first, because
    killing the docker client does not stop it.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin and proc.stdout and proc.stderr  # PIPE was requested for all three
    out, err = bytearray(), bytearray()
    threads = [
        threading.Thread(target=_tail, args=(proc.stdout, out, limit), daemon=True),
        threading.Thread(target=_tail, args=(proc.stderr, err, limit), daemon=True),
        threading.Thread(target=_feed, args=(proc.stdin, stdin or b""), daemon=True),
    ]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        if container:
            _kill_and_wait(container)
        proc.kill()
        proc.wait()
    for t in threads:
        t.join(timeout=2)
    return (-1 if timed_out else proc.returncode), bytes(out), bytes(err), timed_out


def reap_orphans(now: float | None = None) -> list[str]:
    """Remove runner containers whose deadline has passed (their worker died or hung). Returns the names removed.

    Every container carries its own deadline label, so this never touches a live job: a container is only reaped once its job
    should long be over. Safe to call from any worker at any time.
    """
    now = time.time() if now is None else now
    try:
        listed = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label={RUNNER_LABEL}=1", "--format", '{{.Names}}\t{{.Label "evalry.deadline"}}'],
            check=False, capture_output=True, text=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warn("could not list sandbox containers to reap", error=type(e).__name__)  # noqa: G010 (LogQuill API)
        return []
    expired = []
    for line in listed.stdout.splitlines():
        name, _, deadline = line.partition("\t")
        try:
            if float(deadline) < now:
                expired.append(name)
        except ValueError:
            continue  # no readable deadline: not ours to judge
    if expired:
        _kill_and_wait(*expired)
        log.warn("reaped orphaned containers", count=len(expired), containers=",".join(expired))  # noqa: G010 (LogQuill API)
    return expired


def run_python_tests(
    code: str, tests: str, *, timeout_s: float = 10, image: str = PYTHON_IMAGE
) -> SandboxResult:
    with container_slots(1):
        return _run_python_tests(code, tests, timeout_s=timeout_s, image=image)


def _run_python_tests(code: str, tests: str, *, timeout_s: float, image: str) -> SandboxResult:
    """Run ``tests`` against ``code`` in a throwaway container: no network, non-root, read-only root fs, capped resources."""
    name = f"evalry-{uuid.uuid4().hex[:12]}"
    cmd = _docker_cmd(name, image, "CHILD = " + repr(_CHILD) + _BOOT, deadline_s=timeout_s + 30)
    payload = json.dumps({"code": code, "tests": tests})
    code_, out, err, timed_out = run_bounded(
        cmd, stdin=payload.encode(), timeout_s=timeout_s, container=name  # the docker client dying does not stop the container
    )  # fmt: skip
    if timed_out:
        log.warn("sandbox timeout", container=name, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    return SandboxResult(code_, out.decode(errors="replace"), err.decode(errors="replace"), False)


# --- Out-of-process test driver (closes threat T5): solution and tests run in separate containers ---------------------

_MAX_LINE = 1 << 20  # one relayed message; a larger line kills the run
_MAX_MESSAGES = (
    100_000  # messages relayed in one direction; more kills the run (a legitimate test makes far fewer calls)
)
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


def _relay(
    src: IO[bytes],
    dst: IO[bytes],
    violation: list[str],
    stop: threading.Event,
    label: str,
    max_messages: int = _MAX_MESSAGES,
) -> None:
    """Copy JSON lines src -> dst without looking inside them. Never raises; closes dst when src ends."""
    count = 0
    try:
        while line := src.readline(_MAX_LINE + 1):
            count += 1
            if count > max_messages:
                violation.append(f"{label} sent more than {max_messages} messages")
                stop.set()
                break
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
    with container_slots(2):  # solution and tests run in separate containers
        return _run_python_tests_rpc(code, tests, timeout_s=timeout_s, image=image)


def _run_python_tests_rpc(code: str, tests: str, *, timeout_s: float, image: str) -> SandboxResult:
    """Run the solution (container S) and the hidden tests (container T) apart, joined only by a byte relay on the host.

    The tests never exist in any process or filesystem the solution can reach. Exit code is T's verdict; see _driver_t.
    """
    uid = uuid.uuid4().hex[:12]
    names = {"s": f"evalry-s-{uid}", "t": f"evalry-t-{uid}"}
    procs = {
        k: subprocess.Popen(
            _docker_cmd(names[k], image, _driver_source(f"_driver_{k}.py"), deadline_s=timeout_s + 30),
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
