"""Sandbox for repository tasks (plan §7, §9). Same posture as the tier 1 runner, sized for a whole repo's test suite.

No network, non-root, read-only root, capped memory, processes and CPU, tmpfs workspace, no shared volume, scrubbed environment.
The container is handed one validated tar stream (see ``evalry.repo_tasks``); only ``sh`` and ``tar`` run before the test command.
shortcut: edited code and the hidden tests share the container, so a hostile edit can read the hidden tests while they run.
Results are therefore tagged ``inprocess`` isolation and cannot hard-gate; closing it means the two-container design of tier 1.
"""

import subprocess
import time
import uuid
from typing import Self

from evalry.log import get_logger
from evalry.repo_tasks import RepoError
from evalry.runner.sandbox import _MAX_OUTPUT, SEED, SandboxResult, _kill_and_wait

log = get_logger("runner")

MAX_EXPORT_BYTES = 100 * 1024 * 1024
_UNPACK = "mkdir -p /work/repo && tar -xzf - -C /work/repo"


def _docker_base(name: str, image: str, *, detach: bool) -> list[str]:
    return [
        "docker", "run", "--rm", *(["-d"] if detach else ["-i"]), "--name", name,
        "--network", "none", "--read-only", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "1g", "--memory-swap", "1g", "--cpus", "2",
        "--tmpfs", "/work:rw,nosuid,size=512m,mode=1777", "--tmpfs", "/tmp:rw,nosuid,size=64m,mode=1777",
        "--env", "HOME=/work", "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"PYTHONHASHSEED={SEED}",
        "--workdir", "/work", image,
    ]  # fmt: skip


def run_repo(workspace: bytes, command: list[str], image: str, *, timeout_s: float = 120) -> SandboxResult:
    """Unpack ``workspace`` (a tar.gz built by the host) and run ``command`` in it. Exit code 0 means the repo's tests passed."""
    name = f"evalry-r-{uuid.uuid4().hex[:12]}"
    cmd = [
        *_docker_base(name, image, detach=False),
        "sh",
        "-c",
        f'{_UNPACK} && cd /work/repo && exec "$@"',
        "sh",
        *command,
    ]
    try:
        done = subprocess.run(cmd, input=workspace, check=False, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_and_wait(name)
        log.warn("repo sandbox timeout", container=name, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    out, err = done.stdout.decode(errors="replace"), done.stderr.decode(errors="replace")
    return SandboxResult(done.returncode, out[-_MAX_OUTPUT:], err[-_MAX_OUTPUT:], False)


class AgentContainer:
    """A long-lived hardened container an agent works in (tier 4). The agent's commands run with ``docker exec``.

    The hidden tests never enter it: grading happens afterwards in a fresh container from the snapshot plus the agent's diff.
    A command that times out kills the whole container (a process left running inside cannot be trusted), which ends the run.
    """

    def __init__(self, workspace: bytes, image: str) -> None:
        self._workspace, self._image = workspace, image
        self.name = f"evalry-a-{uuid.uuid4().hex[:12]}"
        self.dead = False

    def __enter__(self) -> Self:
        cmd = [*_docker_base(self.name, self._image, detach=True), "sleep", "86400"]
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

    def exec(
        self, argv: list[str], *, stdin: bytes | None = None, timeout_s: float = 60, cwd: str = "/work/repo"
    ) -> SandboxResult:
        if self.dead:
            return SandboxResult(-1, "", "container is gone", True)
        try:
            done = subprocess.run(
                ["docker", "exec", "-i", "--workdir", cwd, self.name, *argv],
                input=stdin or b"", check=False, capture_output=True, timeout=timeout_s,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            self.close()
            return SandboxResult(-1, "", "", True)
        return SandboxResult(
            done.returncode, done.stdout.decode(errors="replace")[-_MAX_OUTPUT:], done.stderr.decode(errors="replace")[-_MAX_OUTPUT:], False
        )  # fmt: skip

    def export(self) -> bytes:
        """The workspace as a tar stream, for the host to validate and diff against the snapshot."""
        deadline = time.monotonic() + 120
        proc = subprocess.Popen(
            ["docker", "exec", self.name, "tar", "-cf", "-", "-C", "/work/repo", "."], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )  # fmt: skip
        assert proc.stdout
        chunks, size = [], 0
        while chunk := proc.stdout.read(1 << 20):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_EXPORT_BYTES or time.monotonic() > deadline:
                proc.kill()
                raise RepoError("The agent's workspace is too large or took too long to export.")
        proc.wait()
        return b"".join(chunks)
