import io
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from evalry.images import PYTHON_IMAGE, check_image
from evalry.repo_tasks import RepoError
from evalry.runner import sandbox
from evalry.runner.repo import run_repo
from evalry.runner.sandbox import (
    _relay,
    acquire_slots,
    container_slots,
    hardening_flags,
    max_containers,
    release_slots,
)
from tests.unit.test_api import Env

OTHER = "node@sha256:" + "d" * 64


# -- F8: which images may run --------------------------------------------------------------------------------------------


def test_only_digest_pinned_allowlisted_images_may_run() -> None:
    check_image(PYTHON_IMAGE, {})  # the built-in image is always allowed
    for tag_only in (
        "python:3.12",
        "python",
        "python@sha256:abc",
        "evil/img@sha256:" + "z" * 64,
        "a b@sha256:" + "a" * 64,
    ):
        with pytest.raises(ValueError, match="pinned by digest"):
            check_image(tag_only, {})
    with pytest.raises(ValueError, match="allowlist"):
        check_image(OTHER, {})
    check_image(OTHER, {"EVALRY_RUNNER_IMAGES": f"x, {OTHER}"})  # exact entry
    check_image(
        "registry.internal/evalry/node@sha256:" + "e" * 64,
        {"EVALRY_RUNNER_IMAGES": "registry.internal/evalry/"},
    )  # prefix
    with pytest.raises(ValueError, match="allowlist"):
        check_image(
            "registry.internal/other/node@sha256:" + "e" * 64,
            {"EVALRY_RUNNER_IMAGES": "registry.internal/evalry/"},
        )
    with pytest.raises(ValueError, match="allowlist"):
        check_image(
            "registry.internal/evalry-evil@sha256:" + "e" * 64,
            {"EVALRY_RUNNER_IMAGES": "registry.internal/evalry"},
        )  # no trailing slash: exact only


def test_the_runner_refuses_an_unlisted_image_before_starting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    monkeypatch.delenv("EVALRY_RUNNER_IMAGES", raising=False)
    with pytest.raises(RepoError, match="allowlist"):
        run_repo(b"", ["true"], OTHER, timeout_s=5)
    assert started == []


def test_suite_creation_rejects_a_task_with_an_unlisted_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALRY_RUNNER_IMAGES", raising=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    env = Env(tmp_path, (key, key.public_key()))
    env.setup_project()

    def suite(image: str) -> dict[str, Any]:
        repo = {"snapshot": "a" * 64, "commit": "abc1234", "image": image, "test_command": ["true"]}
        return {
            "tasks": [
                {
                    "id": "r",
                    "language": "python",
                    "tier": 3,
                    "prompt": "p",
                    "tests": json.dumps({"t.py": "x"}),
                    "repo": repo,
                }
            ]
        }

    bad = env.client.put("/v1/projects/app/suites/v1", json=suite(OTHER), headers=env.h("acme-author"))
    assert bad.status_code == 400 and "allowlist" in bad.json()["error"]
    assert (
        env.client.put(
            "/v1/projects/app/suites/v1", json=suite(PYTHON_IMAGE), headers=env.h("acme-author")
        ).status_code
        == 201
    )
    monkeypatch.setenv("EVALRY_RUNNER_IMAGES", OTHER)
    assert (
        env.client.put(
            "/v1/projects/app/suites/v2", json=suite(OTHER), headers=env.h("acme-author")
        ).status_code
        == 201
    )


# -- F7, F2, F1: limits and the isolation an operator can switch on -------------------------------------------------------


def test_every_container_gets_fd_core_and_file_size_limits_and_the_strict_seccomp_profile() -> None:
    flags = hardening_flags({})
    assert flags[:6] == ["--ulimit", "nofile=1024:1024", "--ulimit", "core=0", "--ulimit", "fsize=268435456"]
    assert (
        flags[6:] == ["--security-opt", f"seccomp={sandbox.SECCOMP_PROFILE}"]
        and sandbox.SECCOMP_PROFILE.is_file()
    )


def test_the_shipped_seccomp_profile_drops_what_a_sandbox_never_needs_and_keeps_the_rest() -> None:
    profile = json.loads(sandbox.SECCOMP_PROFILE.read_text())
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"  # deny by default, like Docker's own
    allowed = {n for s in profile["syscalls"] if s["action"] == "SCMP_ACT_ALLOW" for n in s["names"]}
    assert not allowed & {"ptrace", "process_vm_readv", "process_vm_writev", "name_to_handle_at"}
    assert {
        "read",
        "write",
        "execve",
        "clone",
        "futex",
        "mmap",
        "openat",
        "socketpair",
        "pipe2",
    } <= allowed  # ordinary programs still run
    rules = [
        (s["args"][0]["op"], s["args"][0]["value"]) for s in profile["syscalls"] if "socket" in s["names"]
    ]
    assert sorted(rules) == [
        ("SCMP_CMP_EQ", 10),
        ("SCMP_CMP_LT", 3),
    ]  # AF_UNSPEC/UNIX/INET and INET6 only: no netlink or packet


def test_docker_default_seccomp_is_one_setting_away(tmp_path: Path) -> None:
    assert not any(f.startswith("seccomp=") for f in hardening_flags({"EVALRY_SECCOMP_PROFILE": "default"}))
    custom = tmp_path / "mine.json"
    custom.write_text("{}")
    assert f"seccomp={custom}" in hardening_flags({"EVALRY_SECCOMP_PROFILE": str(custom)})


def test_runtime_seccomp_and_apparmor_are_applied_when_configured(tmp_path: Path) -> None:
    profile = tmp_path / "seccomp.json"
    profile.write_text("{}")
    flags = hardening_flags(
        {
            "EVALRY_DOCKER_RUNTIME": "runsc",
            "EVALRY_SECCOMP_PROFILE": str(profile),
            "EVALRY_APPARMOR_PROFILE": "evalry-sandbox",
        }
    )
    assert ["--runtime", "runsc"] == flags[flags.index("--runtime") : flags.index("--runtime") + 2]
    assert f"seccomp={profile}" in flags and "apparmor=evalry-sandbox" in flags


def test_a_setting_that_cannot_be_honoured_is_an_error_never_weaker_isolation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a file"):
        hardening_flags({"EVALRY_SECCOMP_PROFILE": str(tmp_path / "missing.json")})
    for bad in ("runsc; rm", "--privileged", "a b"):
        with pytest.raises(ValueError, match="runtime"):
            hardening_flags({"EVALRY_DOCKER_RUNTIME": bad})
    for bad in ("x y", "-unconfined"):
        with pytest.raises(ValueError, match="AppArmor"):
            hardening_flags({"EVALRY_APPARMOR_PROFILE": bad})


def test_the_flags_reach_the_docker_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALRY_DOCKER_RUNTIME", "runsc")
    cmd = sandbox._docker_cmd("evalry-x", PYTHON_IMAGE, "pass")
    assert (
        "runsc" in cmd and "core=0" in cmd and cmd.index("--runtime") < cmd.index(PYTHON_IMAGE)
    )  # options before the image


# -- F11: a cap on concurrent containers ---------------------------------------------------------------------------------


@pytest.fixture
def small_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALRY_MAX_CONTAINERS", "3")
    monkeypatch.setattr(sandbox, "_in_use", 0)


def test_the_cap_setting_is_validated() -> None:
    assert max_containers({}) == 16
    for bad in ("0", "1", "-3", "many", ""):
        with pytest.raises(ValueError, match="EVALRY_MAX_CONTAINERS"):
            max_containers({"EVALRY_MAX_CONTAINERS": bad})


def test_jobs_wait_for_a_slot_and_time_out_with_a_retryable_error(small_cap: None) -> None:
    acquire_slots(2)
    with pytest.raises(RuntimeError, match="container limit"):
        acquire_slots(2, wait_s=0.3)  # 2 + 2 > 3
    acquire_slots(1)  # exactly fits
    release_slots(3)
    with container_slots(2):
        pass


def test_a_waiting_job_proceeds_as_soon_as_a_slot_is_released(small_cap: None) -> None:
    acquire_slots(3)
    got: list[float] = []

    def waiter() -> None:
        start = time.monotonic()
        acquire_slots(1, wait_s=10)
        got.append(time.monotonic() - start)
        release_slots(1)

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.3)
    assert got == []  # still waiting
    release_slots(3)
    t.join(5)
    assert len(got) == 1 and 0.2 < got[0] < 5


def test_two_container_jobs_cannot_deadlock_by_each_holding_half(small_cap: None) -> None:
    """Both need 2 of 3 slots; taking them together means one waits whole instead of both holding one."""
    done: list[int] = []

    def job(n: int) -> None:
        with container_slots(2):
            time.sleep(0.2)
            done.append(n)

    threads = [threading.Thread(target=job, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(done) == [0, 1, 2, 3] and sandbox._in_use == 0


# -- F5: the relay has a message-count cap -------------------------------------------------------------------------------


class KeepOpen(io.BytesIO):
    """A destination whose contents can still be read after the relay closes its end."""

    def close(self) -> None:
        pass


def run_relay(lines: int, max_messages: int) -> tuple[list[str], bytes, bool]:
    src, dst = io.BytesIO(b'{"op":"call"}\n' * lines), KeepOpen()
    violation: list[str] = []
    stop = threading.Event()
    _relay(src, dst, violation, stop, "solution", max_messages)
    return violation, dst.getvalue(), stop.is_set()


def test_a_chatty_side_is_stopped_at_the_message_cap() -> None:
    violation, relayed, stopped = run_relay(50, max_messages=10)
    assert stopped and violation == ["solution sent more than 10 messages"]
    assert relayed.count(b"\n") == 10  # nothing past the cap is forwarded


def test_a_side_within_the_cap_is_relayed_untouched() -> None:
    violation, relayed, stopped = run_relay(10, max_messages=10)
    assert not stopped and violation == [] and relayed.count(b"\n") == 10


# -- F14: base images are pinned by digest ---------------------------------------------------------------------------------


def test_service_image_bases_are_pinned_by_digest() -> None:
    root = Path(__file__).parent.parent.parent / "deploy"
    froms = [
        line for line in (root / "Dockerfile.generator").read_text().splitlines() if line.startswith("FROM ")
    ]
    assert froms and all("@sha256:" in line for line in froms)
    compose = (root / "docker-compose.yml").read_text()
    postgres = next(line for line in compose.splitlines() if "image: postgres" in line)
    assert "@sha256:" in postgres
