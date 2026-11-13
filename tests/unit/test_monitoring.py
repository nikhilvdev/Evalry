from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from evalry import cli, monitoring
from evalry.monitoring import MIN_SIGNALS, Signal
from tests.unit.test_api import Env, run_json

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def sig(i: int, task: str, kind: str = "accepted", days_ago: float = 1) -> Signal:
    return Signal(
        signal_id=f"{task}-{i}", task_id=task, kind=kind, observed_at=NOW - timedelta(days=days_ago)
    )  # type: ignore[arg-type]


def batch(task: str, kinds: list[str]) -> list[Signal]:
    return [sig(i, task, k) for i, k in enumerate(kinds)]


def compare(
    offline: dict[str, float], signals: list[Signal], open_tasks: set[str] | None = None
) -> monitoring.MonitorReport:
    return monitoring.compare(
        offline, signals, run_id="r", window_days=30, open_incident_tasks=open_tasks or set(), now=NOW
    )


def test_signals_are_masked_by_schema_there_is_no_room_for_code_or_user_content() -> None:
    for extra in ({"code": "def f(): ..."}, {"user": "alice@example.com"}, {"prompt": "secret"}):
        with pytest.raises(ValidationError):
            Signal(signal_id="s", task_id="t", kind="accepted", observed_at=NOW, **extra)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Signal(signal_id="s", task_id="t", kind="liked", observed_at=NOW)  # type: ignore[arg-type]


def test_spearman_handles_ties_direction_and_undefined_cases() -> None:
    assert monitoring.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert monitoring.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert monitoring.spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)  # ties share a rank
    assert monitoring.spearman([1, 1, 1], [1, 2, 3]) is None  # one side constant
    assert monitoring.spearman([1], [1]) is None


def test_a_task_that_passes_offline_but_fails_in_production_is_a_finding() -> None:
    failing = batch("t", ["reverted"] * 15 + ["build_failed"] * 5 + ["accepted"] * 2)
    r = compare({"t": 1.0, "ok": 1.0}, failing + batch("ok", ["accepted"] * MIN_SIGNALS))
    assert [(f.task_id, f.kind, f.incident_open) for f in r.findings] == [
        ("t", "offline_pass_production_fail", False)
    ]
    assert r.findings[0].production == pytest.approx(2 / 22) and r.tasks_compared == 2
    assert compare({"t": 1.0}, failing, open_tasks={"t"}).findings[0].incident_open


def test_the_reverse_disagreement_is_reported_too_and_agreement_is_quiet() -> None:
    r = compare(
        {"hard": 0.1, "fine": 0.95}, batch("hard", ["accepted"] * 20) + batch("fine", ["accepted"] * 20)
    )
    assert [(f.task_id, f.kind) for f in r.findings] == [("hard", "offline_fail_production_pass")]
    assert compare({"fine": 0.95}, batch("fine", ["accepted"] * 20)).findings == []


def test_too_few_signals_never_produce_a_finding_and_unknown_tasks_are_counted_not_scored() -> None:
    r = compare({"t": 1.0}, batch("t", ["reverted"] * (MIN_SIGNALS - 1)) + batch("ghost", ["accepted"] * 3))
    assert r.findings == [] and r.tasks_compared == 0 and r.unmatched_signals == 3
    assert r.tasks[0].signals == MIN_SIGNALS - 1 and r.tasks[0].production == 0.0


def test_edited_counts_as_half_a_success() -> None:
    r = compare({"t": 0.5}, batch("t", ["edited"] * 20))
    assert r.tasks[0].production == 0.5


def test_rank_correlation_needs_enough_tasks_and_shows_when_offline_predicts_production() -> None:
    offline = {f"t{i}": 0.2 + 0.1 * i for i in range(6)}
    good = [
        s for i in range(6) for s in batch(f"t{i}", ["accepted"] * (i * 3 + 2) + ["reverted"] * (18 - i * 3))
    ]
    assert compare(offline, good).rank_correlation == pytest.approx(1.0)
    assert (
        compare(
            {"t0": 0.5, "t1": 0.6}, batch("t0", ["accepted"] * 20) + batch("t1", ["reverted"] * 20)
        ).rank_correlation
        is None
    )
    assert "NO INCIDENT" in monitoring.render_text(compare({"t": 1.0}, batch("t", ["reverted"] * 20)))


# -- API: the whole loop --------------------------------------------------------------------------------------------------


def api_signals(task: str, kinds: list[str], tag: str = "") -> list[dict[str, Any]]:
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    return [
        {"signal_id": f"{tag}{task}-{i}", "task_id": task, "kind": k, "observed_at": stamp}
        for i, k in enumerate(kinds)
    ]


def test_loop_from_signals_to_finding_to_incident_to_added_tasks(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    env = Env(tmp_path, (key, key.public_key()))
    env.setup_project()
    env.client.put(
        "/v1/projects/app/members/acme-boss", json={"role": "approver"}, headers=env.h("acme-admin")
    )
    base = "/v1/projects/app"
    env.client.post(
        f"{base}/runs", json=run_json("r1"), headers=env.h("acme-author")
    )  # fizzbuzz scores 1.0 offline

    def now_signals() -> dict[str, Any]:
        return {"signals": api_signals("fizzbuzz", ["reverted"] * 22) + api_signals("ghost", ["accepted"])}

    assert (
        env.client.post(
            f"{base}/production-signals", json=now_signals(), headers=env.h("acme-viewer")
        ).status_code
        == 403
    )
    bad = {"signals": [{**api_signals("fizzbuzz", ["accepted"])[0], "code": "def f(): ..."}]}
    assert (
        env.client.post(f"{base}/production-signals", json=bad, headers=env.h("acme-author")).status_code
        == 422
    )  # masked by schema
    first = env.client.post(f"{base}/production-signals", json=now_signals(), headers=env.h("acme-author"))
    assert first.status_code == 202 and first.json() == {"received": 23, "new": 23}
    assert (
        env.client.post(
            f"{base}/production-signals", json=now_signals(), headers=env.h("acme-author")
        ).json()["new"]
        == 0
    )  # idempotent

    rep = env.client.get(f"{base}/production-monitor", headers=env.h("acme-viewer")).json()
    assert rep["signals"] == 23 and rep["unmatched_signals"] == 1 and rep["run_id"] == "r1"
    (finding,) = rep["findings"]
    assert (
        finding["task_id"] == "fizzbuzz"
        and finding["kind"] == "offline_pass_production_fail"
        and not finding["incident_open"]
    )
    health = env.client.get(f"{base}/eval-health", headers=env.h("acme-viewer")).json()
    assert any(
        i["area"] == "production" and i["severity"] == "critical" for i in health["issues"]
    )  # nobody has acted on it yet

    inc = f"{base}/eval-incidents"
    body = {
        "task_id": "fizzbuzz",
        "kind": "offline_pass_production_fail",
        "note": "reverted in production, offline green",
    }
    assert env.client.post(inc, json=body, headers=env.h("acme-author")).status_code == 403
    opened = env.client.post(inc, json=body, headers=env.h("acme-boss"))
    assert opened.status_code == 201
    assert (
        env.client.post(inc, json=body, headers=env.h("acme-boss")).status_code == 409
    )  # one open incident per task
    assert env.client.get(f"{base}/production-monitor", headers=env.h("acme-viewer")).json()["findings"][0][
        "incident_open"
    ]
    health = env.client.get(f"{base}/eval-health", headers=env.h("acme-viewer")).json()
    assert {i["severity"] for i in health["issues"] if i["area"] == "production"} == {
        "warning",
        "info",
    }  # acted on, still open

    close = f"{inc}/{opened.json()['incident_id']}/closure"
    suite = {"tasks": [{"id": "repro-1", "language": "python", "prompt": "p", "tests": "assert True"}]}
    env.client.put(f"{base}/suites/s2", json=suite, headers=env.h("acme-author"))
    for bad_body in (
        {"outcome": "tasks_added", "rationale": "r"},
        {"outcome": "tasks_added", "rationale": "r", "suite_version": "s9", "added_task_ids": ["repro-1"]},
        {"outcome": "tasks_added", "rationale": "r", "suite_version": "s2", "added_task_ids": ["not-there"]},
        {"outcome": "not_an_eval_problem", "rationale": "r", "added_task_ids": ["repro-1"]},
    ):
        assert env.client.post(close, json=bad_body, headers=env.h("acme-boss")).status_code == 400
    good = {
        "outcome": "tasks_added",
        "rationale": "added a task for the reverted case",
        "suite_version": "s2",
        "added_task_ids": ["repro-1"],
    }
    assert env.client.post(close, json=good, headers=env.h("acme-author")).status_code == 403
    assert env.client.post(close, json=good, headers=env.h("acme-boss")).status_code == 201
    assert (
        env.client.post(close, json=good, headers=env.h("acme-boss")).status_code == 409
    )  # closures are immutable
    listed = env.client.get(inc, headers=env.h("acme-viewer")).json()
    assert [(x["state"], x["closure"]["added_task_ids"]) for x in listed] == [("closed", ["repro-1"])]
    assert (
        env.client.post(inc, json=body, headers=env.h("acme-boss")).status_code == 201
    )  # closed, so a new one may open
    actions = [e.action for e in env.audit.events() if e.action.startswith(("incident.", "production."))]
    assert actions == ["production.signals_ingested"] * 2 + [
        "incident.opened",
        "incident.closed",
        "incident.opened",
    ]
    assert (
        env.client.get(f"{base}/production-monitor", headers=env.h("globex-admin", "globex")).status_code
        == 404
    )
    assert (
        env.client.get(f"{base}/production-monitor?days=0", headers=env.h("acme-viewer")).status_code == 422
    )


# -- CLI ------------------------------------------------------------------------------------------------------------------


def test_cli_monitor_prints_and_fails_on_findings_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = compare({"t": 1.0}, batch("t", ["reverted"] * 20)).model_dump_json()

    class Resp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *a: object) -> None: ...

        def read(self) -> bytes:
            return payload.encode()

    seen: dict[str, Any] = {}

    def fake_open(req: Any, timeout: float) -> Resp:
        seen["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_open)
    argv = ["monitor", "--api", "http://localhost:8000", "--project", "app", "--days", "7"]
    assert cli.main(argv) == 2  # no token
    monkeypatch.setenv("EVALRY_TOKEN", "tok")
    assert cli.main(argv) == 0 and "offline pass production fail" in capsys.readouterr().out
    assert seen["url"] == "http://localhost:8000/v1/projects/app/production-monitor?days=7"
    assert cli.main([*argv, "--fail-on-finding"]) == 1
    assert cli.main(["monitor", "--api", "http://remote.example", "--project", "app"]) == 2
