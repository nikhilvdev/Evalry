import json
from collections.abc import Mapping
from typing import Any

import pytest

from evalry import cli
from evalry.runner import nodecheck
from evalry.runner.nodecheck import Check, host_checks, probe_checks, render

GOOD: dict[str, Any] = {
    "uid": 65534, "cap_eff": "0000000000000000", "no_new_privs": "1", "seccomp": "2", "ifaces_up": [], "net": False,
    "write_etc": False, "write_usr": False, "write_work": True, "docker_sock": False, "env_keys": ["HOME", "PATH", "GPG_KEY", "PYTHONHASHSEED"],
    "pids_max": "64", "mem_max": str(256 * 1024 * 1024), "ptrace_blocked": True, "netlink_blocked": True, "root_opts": "ro,relatime",
}  # fmt: skip


def status(checks: list[Check]) -> dict[str, str]:
    """Id to status; where an id repeats (C9 is both host and container) the worst status wins."""
    order = ["pass", "info", "warn", "fail"]
    out: dict[str, str] = {}
    for c in checks:
        if c.id not in out or order.index(c.status) > order.index(out[c.id]):
            out[c.id] = c.status
    return out


def test_a_sandbox_that_meets_every_guarantee_passes_every_probe_check() -> None:
    result = status(probe_checks(GOOD, {}))
    assert set(result.values()) == {"pass"} and set(result) == {
        "C1",
        "C2",
        "C3",
        "C4",
        "C5",
        "C6",
        "C8",
        "C9",
        "C10",
        "R1",
        "R3",
    }


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        ({"uid": 0}, "C6"),
        ({"cap_eff": "00000000a80425fb"}, "C1"),
        ({"no_new_privs": "0"}, "C5"),
        ({"net": True}, "C2"),
        ({"ifaces_up": ["eth0"]}, "C2"),
        ({"write_etc": True}, "C3"),
        ({"write_work": False}, "C3"),
        ({"root_opts": "rw,relatime"}, "C3"),
        ({"docker_sock": True}, "C4"),
        ({"env_keys": ["HOME", "ANTHROPIC_API_KEY"]}, "C8"),
        ({"env_keys": ["EVALRY_DB_WORKER_DSN"]}, "C8"),
        ({"env_keys": ["AWS_SECRET_ACCESS_KEY"]}, "C8"),
        ({"seccomp": "0"}, "C9"),
        ({"ptrace_blocked": False}, "C10"),
        ({"netlink_blocked": False}, "C10"),
        ({"mem_max": "max"}, "R3"),
        ({"mem_max": str(8 * 2**30)}, "R3"),
        ({"pids_max": "max"}, "R1"),
    ],
)
def test_each_broken_guarantee_fails_its_own_check(change: dict[str, Any], failing: str) -> None:
    result = status(probe_checks({**GOOD, **change}, {}))
    assert result[failing] == "fail", change
    assert [i for i, s in result.items() if s == "fail"] == [failing]  # and nothing else is blamed


def test_the_public_gpg_key_id_in_the_python_image_is_not_mistaken_for_a_secret() -> None:
    assert status(probe_checks({**GOOD, "env_keys": ["GPG_KEY"]}, {}))["C8"] == "pass"


def test_choosing_dockers_default_profile_is_a_warning_not_a_silent_pass() -> None:
    weak = {**GOOD, "ptrace_blocked": False, "netlink_blocked": False}
    result = status(probe_checks(weak, {"EVALRY_SECCOMP_PROFILE": "default"}))
    assert result["C10"] == "warn" and result["C9"] == "pass"


def host(**over: Any) -> Mapping[str, object]:
    return {
        "ServerVersion": "29.0",
        "OperatingSystem": "Ubuntu",
        "KernelVersion": "6.8",
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "Runtimes": {"runc": {}},
        **over,
    }


def test_a_root_daemon_on_a_shared_kernel_is_a_recorded_warning_and_isolation_options_pass() -> None:
    assert status(host_checks(host(), {}))["H3"] == "warn"
    assert (
        status(host_checks(host(SecurityOptions=["name=seccomp,profile=builtin", "name=rootless"]), {}))["H3"]
        == "pass"
    )
    assert (
        status(host_checks(host(SecurityOptions=["name=seccomp,profile=builtin", "name=userns"]), {}))["H3"]
        == "pass"
    )
    assert (
        status(host_checks(host(Runtimes={"runc": {}, "runsc": {}}), {"EVALRY_DOCKER_RUNTIME": "runsc"}))[
            "H3"
        ]
        == "pass"
    )
    assert (
        status(host_checks(host(Runtimes={"runc": {}}), {"EVALRY_DOCKER_RUNTIME": "runsc"}))["H3"] == "warn"
    )  # configured but not installed
    assert status(host_checks(host(SecurityOptions=[]), {}))["C9"] == "fail"  # a daemon without seccomp


def test_an_unpinned_allowlist_entry_is_a_failure() -> None:
    assert status(host_checks(host(), {"EVALRY_RUNNER_IMAGES": "node:20"}))["S3"] == "fail"
    assert status(host_checks(host(), {"EVALRY_RUNNER_IMAGES": "registry.internal/evalry/"}))["S3"] == "pass"


def test_render_summarises_and_every_manual_item_is_listed_so_it_is_not_forgotten() -> None:
    text = render([Check("C1", "caps", "pass", "ok"), Check("C2", "net", "fail", "bad"), *nodecheck.MANUAL])
    assert (
        "PASS  C1" in text
        and "FAIL  C2" in text
        and text.splitlines()[-1] == "1 pass, 1 fail, 0 warn, 4 for the reviewer"
    )
    assert {c.id for c in nodecheck.MANUAL} == {"H1", "H5", "H7", "H9"}


# -- the command ---------------------------------------------------------------------------------------------------------------


def test_cli_exit_codes_print_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = [Check("C1", "caps", "pass", "ok")]
    warned = [*ok, Check("H3", "shared", "warn", "w")]
    failed = [Check("C1", "caps", "fail", "bad")]
    answers = iter([ok, warned, warned, failed, ok])
    monkeypatch.setattr(cli, "run_node_checks", lambda: next(answers))
    assert cli.main(["node-check"]) == 0 and "PASS  C1" in capsys.readouterr().out
    assert cli.main(["node-check"]) == 0  # a warning alone does not fail
    assert cli.main(["node-check", "--strict"]) == 1  # unless the node must be isolated
    assert cli.main(["node-check"]) == 1  # a failure always does
    capsys.readouterr()
    assert cli.main(["node-check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"id": "C1", "what": "caps", "status": "pass", "detail": "ok"}
    ]


def test_worker_refuses_to_start_on_a_failing_node_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EVALRY_DB_WORKER_DSN", "host=nowhere")
    replies = tmp_path / "r.json"
    replies.write_text("{}")
    monkeypatch.setattr(
        cli, "run_node_checks", lambda: [Check("C2", "No network", "fail", "an interface is up")]
    )
    assert cli.main(["worker", "--once", "--replies", str(replies), "--require-node-check"]) == 2
    err = capsys.readouterr().err
    assert "does not meet the sandbox requirements" in err and "C2" in err and "an interface is up" in err


def test_a_missing_docker_is_one_clear_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodecheck, "_docker_info", lambda: None)
    checks = nodecheck.run_node_checks()
    assert len(checks) == 1 and checks[0].status == "fail" and "docker info" in checks[0].detail
