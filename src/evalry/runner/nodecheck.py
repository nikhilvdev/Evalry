"""Runner node posture check (security review F1; checklist items H and C).

Starts one probe container with the exact flags every sandbox job gets, and reports what the container really sees: its
capabilities, network, filesystem, limits, seccomp mode and environment. It also reads what the Docker host says about its own
isolation (rootless, user namespaces, a user-space runtime) and the Docker socket's permissions. A FAIL means a sandbox
guarantee does not hold on this node; a WARN is a recorded decision for the reviewer (typically: containers share the kernel of a
root daemon). Nothing here changes the node. Items that cannot be observed from the node (who else runs there, egress rules,
patch ownership) are listed as manual so they are not forgotten.
"""

import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from evalry.images import PINNED, PYTHON_IMAGE, allowed_entries
from evalry.runner.sandbox import SECCOMP_PROFILE, _docker_cmd, max_containers, run_bounded

Status = Literal["pass", "fail", "warn", "info"]
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|DSN|EVALRY|ANTHROPIC|AWS_", re.IGNORECASE)
_PUBLIC_VARS = {"GPG_KEY"}  # the official Python image sets the public signing key id of its release manager

# Runs inside the container as the sandbox user. Prints one JSON object of observations and nothing else.
PROBE = r"""
import ctypes, json, os, socket
def field(key):
    for line in open('/proc/self/status'):
        if line.startswith(key + ':'):
            return line.split(':', 1)[1].strip()
def read(path):
    try:
        return open(path).read().strip()
    except OSError:
        return None
def writable(path):
    try:
        open(path, 'w').write('x')
        return True
    except OSError:
        return False
o = {'uid': os.getuid(), 'cap_eff': field('CapEff'), 'no_new_privs': field('NoNewPrivs'), 'seccomp': field('Seccomp')}
o['ifaces_up'] = sorted(i for i in os.listdir('/sys/class/net') if i != 'lo' and int(read('/sys/class/net/%s/flags' % i) or '0', 16) & 1)
try:
    socket.create_connection(('1.1.1.1', 53), timeout=2)
    o['net'] = True
except OSError:
    o['net'] = False
o['write_etc'], o['write_usr'], o['write_work'] = writable('/etc/evalry-probe'), writable('/usr/evalry-probe'), writable('/work/probe')
o['docker_sock'] = any(os.path.exists(p) for p in ('/var/run/docker.sock', '/run/docker.sock'))
o['env_keys'] = sorted(os.environ)
o['pids_max'] = read('/sys/fs/cgroup/pids.max') or read('/sys/fs/cgroup/pids/pids.max')
o['mem_max'] = read('/sys/fs/cgroup/memory.max') or read('/sys/fs/cgroup/memory/memory.limit_in_bytes')
libc = ctypes.CDLL(None, use_errno=True)
o['ptrace_blocked'] = libc.ptrace(0, 0, 0, 0) != 0
try:
    socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    o['netlink_blocked'] = False
except OSError:
    o['netlink_blocked'] = True
o['root_opts'] = next((l.split()[3] for l in open('/proc/mounts') if l.split()[1] == '/'), '')
print(json.dumps(o))
"""


@dataclass(frozen=True)
class Check:
    id: str  # the review checklist item this evidences
    what: str
    status: Status
    detail: str


def _docker_info() -> dict[str, object] | None:
    try:
        done = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _probe(image: str) -> dict[str, object]:
    name = f"evalry-probe-{uuid.uuid4().hex[:8]}"
    code, out, err, timed_out = run_bounded(
        _docker_cmd(name, image, PROBE, deadline_s=90), stdin=None, timeout_s=60, container=name
    )
    if timed_out or code != 0:
        raise RuntimeError(
            f"The probe container did not run (exit {code}): {err.decode(errors='replace')[-300:] or 'timed out'}"
        )
    parsed: dict[str, object] = json.loads(out.decode().strip().splitlines()[-1])
    return parsed


def _c(id_: str, what: str, ok: bool, good: str, bad: str, *, warn: bool = False) -> Check:
    return Check(id_, what, "pass" if ok else ("warn" if warn else "fail"), good if ok else bad)


def host_checks(info: Mapping[str, object], env: Mapping[str, str]) -> list[Check]:
    sec = [str(s) for s in (info.get("SecurityOptions") or [])]  # type: ignore[attr-defined]
    runtimes = set(info.get("Runtimes") or {})  # type: ignore[call-overload]
    runtime = env.get("EVALRY_DOCKER_RUNTIME")
    checks = [
        Check(
            "H4",
            "Docker engine and kernel are known and supported",
            "info",
            f"Docker {info.get('ServerVersion')} on {info.get('OperatingSystem')}, kernel {info.get('KernelVersion')}. Confirm they are patched and who owns updates.",
        )
    ]
    isolated = any("rootless" in s or "userns" in s for s in sec) or bool(
        runtime and runtime in runtimes and runtime != "runc"
    )
    checks.append(
        _c(
            "H3",
            "Containers are isolated from a root daemon on a shared kernel",
            isolated,
            "rootless, user-namespace remapping or a user-space runtime is active",
            "containers share the host kernel and the daemon runs as root: record this accepted risk (finding F1) or enable rootless Docker, userns-remap or EVALRY_DOCKER_RUNTIME=runsc",
            warn=True,
        )
    )
    checks.append(
        _c(
            "C9",
            "The daemon has seccomp enabled",
            any("seccomp" in s for s in sec),
            "seccomp is enabled",
            "the daemon runs without seccomp",
        )
    )
    sock = Path("/var/run/docker.sock")
    if not env.get("DOCKER_HOST") and sock.exists():
        mode = sock.stat().st_mode
        owner = f"{sock.owner()}:{sock.group()} {stat.filemode(mode)}"
        checks.append(
            _c(
                "H2",
                "The Docker socket is not world-accessible",
                not mode & 0o006,
                f"socket is {owner}; access is root-equivalent, so keep the group small",
                f"socket is {owner}: any local user could start containers as root",
            )
        )
    else:
        checks.append(
            Check(
                "H2",
                "Docker socket access",
                "info",
                "a remote or non-default daemon is in use: review who can reach it by hand",
            )
        )
    entries = allowed_entries(env)
    unpinned = [e for e in entries if not e.endswith("/") and not PINNED.fullmatch(e)]
    checks.append(
        _c(
            "S3",
            "The runner image allowlist is digest-pinned",
            not unpinned,
            f"{len(entries)} allowed entries, all pinned or registry prefixes",
            f"not pinned by digest: {unpinned}",
        )
    )
    checks.append(
        Check(
            "H8",
            "Container cap",
            "info",
            f"EVALRY_MAX_CONTAINERS is {max_containers(env)} per worker process; run one worker process per node, or split it",
        )
    )
    return checks


def probe_checks(o: Mapping[str, object], env: Mapping[str, str]) -> list[Check]:
    mem, pids = str(o.get("mem_max")), str(o.get("pids_max"))
    custom = (env.get("EVALRY_SECCOMP_PROFILE") or str(SECCOMP_PROFILE)) != "default"
    secrets = sorted(k for k in o["env_keys"] if _SECRET_NAME.search(k) and k not in _PUBLIC_VARS)  # type: ignore[attr-defined]
    return [
        _c(
            "C6",
            "The sandbox runs as a non-root user",
            o["uid"] != 0,
            f"uid {o['uid']}",
            "the sandbox runs as root",
        ),
        _c(
            "C1",
            "No Linux capabilities",
            set(str(o["cap_eff"])) <= {"0"},
            "effective capabilities are empty",
            f"effective capabilities are {o['cap_eff']}",
        ),
        _c(
            "C5",
            "no-new-privileges is set",
            o["no_new_privs"] == "1",
            "NoNewPrivs=1",
            f"NoNewPrivs={o['no_new_privs']}",
        ),
        _c(
            "C2",
            "No network inside the sandbox",
            not o["ifaces_up"] and o["net"] is False,
            "no interface besides loopback is up, and an outbound connection fails",
            f"interfaces up besides loopback: {o['ifaces_up']}; outbound connection {'succeeded' if o['net'] else 'failed'}",
        ),
        _c(
            "C3",
            "The root filesystem is read-only; only the workspace is writable",
            "ro" in str(o["root_opts"]).split(",")
            and not o["write_etc"]
            and not o["write_usr"]
            and bool(o["write_work"]),
            "writes to /etc and /usr fail, /work is writable",
            f"root mount options {o['root_opts']!r}; /etc writable {o['write_etc']}, /usr writable {o['write_usr']}, /work writable {o['write_work']}",
        ),
        _c(
            "C4",
            "The Docker socket is not inside the sandbox",
            not o["docker_sock"],
            "not present",
            "a Docker socket is mounted in the sandbox",
        ),
        _c(
            "C8",
            "No credentials in the sandbox environment",
            not secrets,
            "no secret-like variables",
            f"secret-like variables present: {secrets}",
        ),
        _c(
            "C9",
            "Seccomp filtering is active for the sandbox process",
            o["seccomp"] == "2",
            "Seccomp mode 2 (filter)",
            f"Seccomp mode is {o['seccomp']}",
        ),
        _c(
            "C10",
            "The strict seccomp profile is in force (ptrace and netlink sockets refused)",
            bool(o["ptrace_blocked"] and o["netlink_blocked"]) if custom else False,
            "ptrace and AF_NETLINK are refused",
            "Docker's default profile is in use (EVALRY_SECCOMP_PROFILE=default): ptrace and netlink sockets are allowed"
            if not custom
            else f"the strict profile is configured but ptrace_blocked={o['ptrace_blocked']}, netlink_blocked={o['netlink_blocked']}",
            warn=not custom,
        ),
        _c(
            "R3",
            "A memory limit applies",
            mem.isdigit() and int(mem) <= 256 * 1024 * 1024,
            f"limit {int(mem) // 2**20} MB" if mem.isdigit() else "",
            f"memory limit reads {mem!r}",
        ),
        _c(
            "R1",
            "A process limit applies",
            pids.isdigit() and int(pids) <= 64,
            f"limit {pids} processes",
            f"process limit reads {pids!r}",
        ),
    ]


MANUAL = [
    Check(
        "H1",
        "The node runs nothing but the worker and Docker",
        "info",
        "manual: list the node's processes and listeners",
    ),
    Check(
        "H5",
        "Worker credentials are least privilege",
        "info",
        "manual: review the worker's environment and database grants",
    ),
    Check(
        "H7",
        "Outbound traffic from the node is allowlisted",
        "info",
        "manual: review the firewall or security group; try an external connection from the node",
    ),
    Check(
        "H9",
        "Orphaned containers are reaped",
        "info",
        "manual: kill a worker mid-run, then check that evalry reap (cron) clears the containers",
    ),
]


def run_node_checks(image: str = PYTHON_IMAGE, env: Mapping[str, str] | None = None) -> list[Check]:
    env = os.environ if env is None else env
    info = _docker_info()
    if info is None:
        return [
            Check(
                "H3",
                "Docker is reachable",
                "fail",
                "could not run `docker info`: is Docker running and is this user allowed to use it?",
            )
        ]
    checks = host_checks(info, env)
    try:
        checks += probe_checks(_probe(image), env)
    except (RuntimeError, ValueError, KeyError, IndexError) as e:
        checks.append(Check("C1", "The probe container ran", "fail", str(e)))
    return checks + MANUAL


def render(checks: list[Check]) -> str:
    tag = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "info": "INFO"}
    lines = [f"{tag[c.status]:4}  {c.id:4} {c.what}: {c.detail}" for c in checks]
    counts = {s: sum(c.status == s for c in checks) for s in tag}
    lines.append(
        f"{counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn, {counts['info']} for the reviewer"
    )
    return "\n".join(lines)
