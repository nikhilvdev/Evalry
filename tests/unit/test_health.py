import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Self

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from evalry import cli, health
from evalry.disputes import Dispute, Resolution
from evalry.gates.hard import GateDecision
from evalry.judge.calibration import CalibrationRecord
from evalry.schemas import RunRecord, SampleResult, Task
from tests.unit.test_api import Env, run_json

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def sample(
    task: str, score: float, code: str = "c", cost: float = 0.0, judge: str | None = None
) -> SampleResult:
    return SampleResult(
        task_id=task, task_hash=f"h-{task}", sample=0, generator_model="m", code=code, scores=[], composite=score,
        cost_usd=cost, judge_version=judge,
    )  # fmt: skip


def run(run_id: str, results: list[SampleResult], days_ago: int = 0, suite: str = "s1") -> RunRecord:
    return RunRecord(
        run_id=run_id, suite_version=suite, started_at=NOW - timedelta(days=days_ago), results=results
    )


def cal(version: str, agreement: float, age: int, passed: bool = True) -> CalibrationRecord:
    return CalibrationRecord(
        judge_version=version, set_hash="a" * 64, items=24, agreement_within_one=agreement, stability=1,
        injection_resistant=True, passed=passed, calibration_id=f"{version}{age}", created_at=NOW - timedelta(days=age),
        created_by="x",
    )  # fmt: skip


def decision(did: str, outcome: str) -> GateDecision:
    return GateDecision(
        decision_id=did, gate="g", rule_hash="h", run_id="r", suite_version="s1", environment="dev", outcome=outcome,  # type: ignore[arg-type]
        reasons=[], waived=[], pass_rate=0.0, samples=1, actor="a", decided_at=NOW,
    )  # fmt: skip


def compute(**kw: Any) -> health.EvalHealth:
    args: dict[str, Any] = {
        "runs": [],
        "suites": {},
        "calibrations": [],
        "decisions": [],
        "disputes": [],
        "now": NOW,
    }
    return health.compute(**{**args, **kw})


def areas(h: health.EvalHealth, severity: str | None = None) -> list[str]:
    return [i.area for i in h.issues if severity in (None, i.severity)]


def test_an_empty_project_reports_nothing_rather_than_inventing_numbers() -> None:
    h = compute()
    assert (
        h.runs,
        h.flakiness.rate,
        h.saturation.saturated_share,
        h.gates.confirmed_real_rate,
        h.issues,
    ) == (0, None, None, None, [])


def test_flakiness_is_the_same_code_scoring_differently_across_runs() -> None:
    a = run("a", [sample("t1", 1.0, "x"), sample("t2", 1.0, "y"), sample("t3", 0.0, "z")], days_ago=2)
    b = run("b", [sample("t1", 0.0, "x"), sample("t2", 1.0, "y"), sample("t3", 0.0, "other")], days_ago=1)
    f = compute(runs=[a, b]).flakiness
    assert (f.solutions_regraded, f.flaky, f.rate) == (
        2,
        1,
        0.5,
    )  # t1 flipped, t2 stable, t3 had different code
    assert "flakiness" in areas(compute(runs=[a, b]), "critical")  # 50% is over the critical line


def test_saturation_and_never_solved_tasks() -> None:
    r = run("a", [sample(f"t{i}", 1.0) for i in range(8)] + [sample("hard", 0.0), sample("easy", 0.96)])
    h = compute(runs=[r])
    assert h.saturation.saturated_share == 0.9 and h.saturation.never_solved == ["hard"]
    assert {"saturation", "task quality"} <= set(areas(h, "warning"))


def test_coverage_counts_and_flags_thin_tags_and_small_suites() -> None:
    tasks = [
        Task(id=f"t{i}", language="python", prompt="p", tests="t", tags=["io"] if i < 5 else ["rare"])
        for i in range(6)
    ]
    h = compute(runs=[run("a", [sample("t0", 1.0)])], suites={"s1": tasks})
    assert h.coverage.by_language == {"python": 6} and h.coverage.by_tag == {"io": 5, "rare": 1}
    assert h.coverage.thin_tags == ["rare"] and not h.coverage.regression_gate_ready
    assert areas(h, "info") == ["coverage", "coverage"]


def test_judge_health_flags_failing_stale_expired_and_dropping_calibrations() -> None:
    h = compute(calibrations=[
        cal("bad", 0.5, 1, passed=False), cal("stale", 0.9, 70), cal("expired", 0.9, 95),
        cal("slide", 0.95, 20), cal("slide", 0.80, 1),
    ])  # fmt: skip
    msgs = {i.message.split("'s")[0].split()[1] + ":" + i.severity for i in h.issues if i.area == "judge"}
    assert {"bad:critical", "stale:warning", "expired:critical", "slide:warning"} <= msgs
    slide = next(j for j in h.judges if j.judge_version == "slide")
    assert slide.agreement_change == pytest.approx(-0.15) and slide.current


def test_uncalibrated_judge_use_is_noted_as_advisory() -> None:
    h = compute(runs=[run("a", [sample("t", 1.0, judge="jv-new")])], calibrations=[cal("jv-old", 0.9, 1)])
    assert any("jv-new" in i.message and i.severity == "info" for i in h.issues)


def test_gates_report_how_many_blocks_stood_after_dispute() -> None:
    def d(i: str) -> Dispute:
        return Dispute(decision_id=i, run_id="r", opened_by="a", reason="x", task_ids=["t"], created_at=NOW)

    def res(i: str, outcome: str) -> Resolution:
        return Resolution(decision_id=i, resolved_by="b", outcome=outcome, rationale="x", created_at=NOW)  # type: ignore[arg-type]

    decisions = [decision(f"d{i}", "block") for i in range(4)] + [decision("ok", "pass")]
    disputes = [(d("d0"), res("d0", "exception_approved")), (d("d1"), res("d1", "task_corrected")),
                (d("d2"), res("d2", "rejected")), (d("d3"), None)]  # fmt: skip
    h = compute(decisions=decisions, disputes=disputes)
    g = h.gates
    assert (g.decisions, g.blocked, g.disputes_open, g.disputes_resolved, g.overturned) == (5, 4, 1, 3, 2)
    assert g.confirmed_real_rate == 0.5
    assert "gates" in areas(h, "warning")  # under the 80% target


def test_cost_per_run_and_per_solved_task() -> None:
    r = run("a", [sample("t1", 1.0, cost=0.25), sample("t2", 0.0, cost=0.75)])
    c = compute(runs=[r, run("none", [sample("t", 0.0, cost=1.0)], days_ago=1)])
    assert [(x.run_id, x.cost_per_solved_usd) for x in c.cost] == [
        ("none", None),
        ("a", 1.0),
    ] and c.total_cost_usd == 2.0


def test_issues_are_sorted_most_severe_first() -> None:
    h = compute(calibrations=[cal("bad", 0.5, 1, passed=False), cal("stale", 0.9, 70)],
                runs=[run("a", [sample("t", 0.0)])], suites={"s1": []})  # fmt: skip
    sev = [i.severity for i in h.issues]
    assert sev == sorted(sev, key=["critical", "warning", "info"].index)


def test_html_dashboard_escapes_content_and_loads_nothing_external() -> None:
    evil = '<script>alert("x")</script>'
    page = health.render_html(
        compute(runs=[run("a", [sample(evil, 0.0)])], calibrations=[cal(evil, 0.5, 1, passed=False)])
    )
    assert evil not in page and "&lt;script&gt;" in page
    assert "<script" not in page and "http://" not in page and "https://" not in page
    assert "prefers-color-scheme" in page and "width=device-width" in page
    assert "Eval health" in health.render_text(compute())


# -- API and CLI ----------------------------------------------------------------------------------------------------------


def test_endpoint_is_readable_by_a_viewer_and_tenant_scoped(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    env = Env(tmp_path, (key, key.public_key()))
    env.setup_project()
    env.client.post("/v1/projects/app/runs", json=run_json("r1"), headers=env.h("acme-author"))
    r = env.client.get("/v1/projects/app/eval-health", headers=env.h("acme-viewer"))
    assert r.status_code == 200 and r.json()["runs"] == 1 and r.json()["saturation"]["suite_version"] == "s1"
    assert (
        env.client.get("/v1/projects/app/eval-health", headers=env.h("globex-admin", "globex")).status_code
        == 404
    )
    assert env.client.get("/v1/projects/app/eval-health").status_code == 401


def test_cli_health_prints_writes_html_and_sets_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = compute(
        runs=[run("a", [sample("t", 0.0)])], calibrations=[cal("bad", 0.5, 1, passed=False)]
    ).model_dump_json()

    class Resp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *a: object) -> None: ...

        def read(self) -> bytes:
            return payload.encode()

    seen: dict[str, Any] = {}

    def fake_open(req: Any, timeout: float) -> Resp:
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(cli.urllib.request, "urlopen", fake_open)
    base = ["health", "--api", "http://localhost:8000", "--project", "app"]
    assert cli.main(base) == 2  # no token
    monkeypatch.setenv("EVALRY_TOKEN", "tok")
    assert cli.main(base) == 0 and "Eval health" in capsys.readouterr().out
    assert seen == {"url": "http://localhost:8000/v1/projects/app/eval-health", "auth": "Bearer tok"}
    out = tmp_path / "h.html"
    assert (
        cli.main([*base, "--html", str(out), "--fail-on", "critical"]) == 1
    )  # the failing judge is critical
    assert "<title>Eval health</title>" in out.read_text()
    assert (
        cli.main(["health", "--api", "http://remote.example", "--project", "app"]) == 2
    )  # http off localhost is refused
    assert json.loads(payload)["runs"] == 1
