"""Sandbox for repository tasks (plan §7, §9). Same posture as the tier 1 runner, sized for a whole repo's test suite.

No network, non-root, read-only root, capped memory, processes and CPU, tmpfs workspace, no shared volume, scrubbed environment.
The container is handed one validated tar stream (see ``evalry.repo_tasks``); only ``sh`` and ``tar`` run before the test command.
shortcut: edited code and the hidden tests share the container, so a hostile edit can read the hidden tests while they run.
Results are therefore tagged ``inprocess`` isolation and cannot hard-gate; closing it means the two-container design of tier 1.
"""

import subprocess
import threading
import uuid
from typing import Self

from evalry.images import check_image
from evalry.log import get_logger
from evalry.repo_tasks import RepoError
from evalry.runner.sandbox import (
    SEED,
    SandboxResult,
    _kill_and_wait,
    acquire_slots,
    container_labels,
    container_slots,
    release_slots,
    run_bounded,
)

log = get_logger("runner")

MAX_EXPORT_BYTES = 100 * 1024 * 1024
EXPORT_TIMEOUT_S = 120
GRACE_S = 30  # how long past its limit a container may live before the reaper treats it as orphaned
WATCHDOG_SLACK_S = 15  # the in-container watchdog fires this long after the host's own timeout would have
_UNPACK = "mkdir -p /work/repo && tar -xzf - -C /work/repo"


def _docker_base(name: str, image: str, *, detach: bool, deadline_s: float) -> list[str]:
    return [
        "docker", "run", "--rm", *(["-d"] if detach else ["-i"]), "--name", name, *container_labels(deadline_s),
        "--network", "none", "--read-only", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--cpus", "2",
        "--tmpfs", "/work:rw,nosuid,size=512m,mode=1777", "--tmpfs", "/tmp:rw,nosuid,size=64m,mode=1777",
        "--env", "HOME=/work", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"PYTHONHASHSEED={SEED}",
        "--workdir", "/work", image,
    ]  # fmt: skip


def _check_image(image: str) -> None:
    try:
        check_image(image)
    except ValueError as e:
        raise RepoError(str(e)) from e


def _repo_script(timeout_s: float) -> str:
    """Unpack, then run the command as a background job under a watchdog.

    If the host dies mid-run nothing outside can stop the container, so it stops itself: the watchdog kills the command after
    ``timeout_s`` plus a slack, PID 1 (this shell) then exits and Docker removes the container. The command's stdin is
    /dev/null (the tar stream was already consumed) and its exit status is passed through.
    """
    limit = int(timeout_s + WATCHDOG_SLACK_S)
    return f'{_UNPACK} && cd /work/repo && {{ "$@" & pid=$!; (sleep {limit}; kill -9 $pid) 2>/dev/null & wait $pid; }}'


def repo_command(name: str, command: list[str], image: str, timeout_s: float) -> list[str]:
    return [
        *_docker_base(name, image, detach=False, deadline_s=timeout_s + GRACE_S),
        "sh", "-c", _repo_script(timeout_s), "sh", *command,
    ]  # fmt: skip


def run_repo(workspace: bytes, command: list[str], image: str, *, timeout_s: float = 120) -> SandboxResult:
    """Unpack ``workspace`` (a tar.gz built by the host) and run ``command`` in it. Exit code 0 means the repo's tests passed.

    Output is read through a bounded tail, so a test that floods stdout costs the host a fixed amount of memory.
    """
    _check_image(image)
    name = f"evalry-r-{uuid.uuid4().hex[:12]}"
    with container_slots(1):
        code, out, err, timed_out = run_bounded(
            repo_command(name, command, image, timeout_s),
            stdin=workspace,
            timeout_s=timeout_s,
            container=name,
        )
    if timed_out:
        log.warn("repo sandbox timeout", container=name, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    return SandboxResult(code, out.decode(errors="replace"), err.decode(errors="replace"), False)


class AgentContainer:
    """A long-lived hardened container an agent works in (tier 4). The agent's commands run with ``docker exec``.

    The hidden tests never enter it: grading happens afterwards in a fresh container from the snapshot plus the agent's diff.
    A command that times out kills the whole container (a process left running inside cannot be trusted), which ends the run.
    """

    def __init__(self, workspace: bytes, image: str, *, lifetime_s: float = 1800) -> None:
        """``lifetime_s`` is a hard ceiling: the container's own process ends then, so it cannot outlive a dead worker."""
        self._workspace, self._image, self._lifetime = workspace, image, lifetime_s
        self.name = f"evalry-a-{uuid.uuid4().hex[:12]}"
        self.dead = False
        self._slot = False

    def __enter__(self) -> Self:
        _check_image(self._image)
        acquire_slots(1)  # held for the container's whole life
        self._slot = True
        cmd = [
            *_docker_base(self.name, self._image, detach=True, deadline_s=self._lifetime + GRACE_S),
            "sleep",
            str(int(self._lifetime)),
        ]
        started = subprocess.run(cmd, check=False, capture_output=True, timeout=60)
        if started.returncode != 0:
            raise RepoError(
                f"Could not start the agent container: {started.stderr.decode(errors='replace')[:300]}"
            )
        unpacked = self.exec(["sh", "-c", _UNPACK], stdin=self._workspace, timeout_s=60, cwd="/work")
        if unpacked.exit_code != 0:
            self.close()
            raise RepoError(f"Could not unpack the workspace: {unpacked.stderr[:300]}")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self.dead:
            self.dead = True
            _kill_and_wait(self.name)
        if self._slot:
            self._slot = False
            release_slots(1)

    def exec(
        self, argv: list[str], *, stdin: bytes | None = None, timeout_s: float = 60, cwd: str = "/work/repo"
    ) -> SandboxResult:
        if self.dead:
            return SandboxResult(-1, "", "container is gone", True)
        code, out, err, timed_out = run_bounded(
            ["docker", "exec", "-i", "--workdir", cwd, self.name, *argv], stdin=stdin, timeout_s=timeout_s
        )
        if timed_out:
            self.close()  # a process left running in there cannot be trusted
            return SandboxResult(-1, "", "", True)
        return SandboxResult(code, out.decode(errors="replace"), err.decode(errors="replace"), False)

    def export(self) -> bytes:
        """The workspace as a tar stream, for the host to validate and diff against the snapshot.

        Bounded in size, and in time by a timer that kills the export even if ``tar`` stalls without producing output.
        """
        proc = subprocess.Popen(
            ["docker", "exec", self.name, "tar", "-cf", "-", "-C", "/work/repo", "."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )  # fmt: skip
        assert proc.stdout
        timer = threading.Timer(EXPORT_TIMEOUT_S, proc.kill)
        timer.start()
        chunks, size = [], 0
        try:
            while chunk := proc.stdout.read(1 << 20):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_EXPORT_BYTES:
                    proc.kill()
                    raise RepoError("The agent's workspace is too large to export.")
            proc.wait()
            if not timer.is_alive():
                raise RepoError("The agent's workspace export took too long.")
        finally:
            timer.cancel()
            proc.kill()
            proc.wait()
        return b"".join(chunks)
