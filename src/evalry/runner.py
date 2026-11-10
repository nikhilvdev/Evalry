"""Docker sandbox for untrusted code (plan §7, §9). Model-generated code never runs outside this module."""

import json
import subprocess
import uuid
from typing import NamedTuple

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


def run_python_tests(
    code: str, tests: str, *, timeout_s: float = 10, image: str = PYTHON_IMAGE
) -> SandboxResult:
    """Run ``tests`` against ``code`` in a throwaway container: no network, non-root, read-only root fs, capped resources."""
    name = f"evalry-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none", "--read-only", "--user", "65534:65534",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--memory", "256m", "--memory-swap", "256m", "--cpus", "1",
        "--tmpfs", "/work:rw,nosuid,size=16m,mode=1777",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"PYTHONHASHSEED={SEED}",
        image, "python", "-c", "CHILD = " + repr(_CHILD) + _BOOT,
    ]  # fmt: skip
    payload = json.dumps({"code": code, "tests": tests})
    try:
        done = subprocess.run(
            cmd, input=payload, check=False, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        # The docker client dying does not stop the container, so kill it by name.
        subprocess.run(["docker", "kill", name], check=False, capture_output=True)
        log.warn("sandbox timeout", container=name, timeout_s=timeout_s)  # noqa: G010 (LogQuill API)
        return SandboxResult(-1, "", "", True)
    return SandboxResult(done.returncode, done.stdout[:_MAX_OUTPUT], done.stderr[:_MAX_OUTPUT], False)
