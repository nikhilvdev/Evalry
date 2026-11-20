"""Eval health (plan §6, §11, §14): can this project's scores be trusted enough to block a release on?

Everything here is derived from records the platform already keeps (runs, suites, gate decisions, calibrations, disputes), so
the report is reproducible and needs no new instrumentation. It reports measured facts plus ``issues``: the open eval-health
problems, each with a severity. Thresholds are this module's choice and are named constants, to tune with the eval owner.
Not measured yet: time to result (runs do not record a finish time), drift on a stability set (no such set is defined yet),
and production-versus-offline comparison.
"""

import hashlib
import html
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evalry.disputes import Dispute, Resolution
from evalry.gates.hard import GateDecision
from evalry.judge.calibration import AGREEMENT_TARGET, MAX_AGE, CalibrationRecord, passing_versions
from evalry.monitoring import MonitorReport
from evalry.schemas import RunRecord, Task

SATURATED_MEAN = 0.95  # a task every sample solves tells you nothing about a change
SATURATED_SUITE_SHARE = 0.8  # a suite is saturated when this share of its tasks is
FLAKY_CRITICAL = 0.05
THIN_TAG = 3  # fewer tasks than this under a tag is a coverage gap
REGRESSION_GATE_TASKS = 100  # plan §6: regression gates need at least 100 tasks
AGREEMENT_DROP = 0.05
STALE_AFTER = MAX_AGE * 2 // 3  # warn well before a calibration stops counting


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Issue(_Strict):
    severity: Literal["critical", "warning", "info"]
    area: str
    message: str


class RunCost(_Strict):
    run_id: str
    started_at: datetime
    samples: int
    cost_usd: float
    solved: int
    cost_per_solved_usd: float | None


class Flakiness(_Strict):
    solutions_regraded: int
    flaky: int
    rate: float | None


class Saturation(_Strict):
    suite_version: str | None
    tasks: int
    saturated_share: float | None
    never_solved: list[str]


class Coverage(_Strict):
    suite_version: str | None
    tasks: int
    by_language: dict[str, int]
    by_tier: dict[int, int]
    by_tag: dict[str, int]
    thin_tags: list[str]
    regression_gate_ready: bool


class JudgeHealth(_Strict):
    judge_version: str
    agreement_within_one: float
    passed: bool
    age_days: int
    current: bool
    agreement_change: float | None


class GateHealth(_Strict):
    decisions: int
    blocked: int
    disputes_open: int
    disputes_resolved: int
    overturned: int
    confirmed_real_rate: float | None  # share of blocks not overturned by a dispute (KPI target 80%+)


class EvalHealth(_Strict):
    generated_at: datetime
    runs: int
    flakiness: Flakiness
    saturation: Saturation
    coverage: Coverage
    judges: list[JudgeHealth]
    gates: GateHealth
    cost: list[RunCost]
    total_cost_usd: float
    issues: list[Issue]


def _flakiness(runs: list[RunRecord]) -> Flakiness:
    """Same task, same code, different score across runs: the grader (or its environment) is not deterministic."""
    seen: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in runs:
        for s in r.results:
            ident = s.code_digest or hashlib.sha256(s.code.encode()).hexdigest()
            seen[(s.task_hash, ident)].append(s.composite)
    regraded = [v for v in seen.values() if len(v) > 1]
    flaky = sum(len(set(v)) > 1 for v in regraded)
    return Flakiness(
        solutions_regraded=len(regraded), flaky=flaky, rate=flaky / len(regraded) if regraded else None
    )


def _saturation(run: RunRecord | None) -> Saturation:
    if run is None:
        return Saturation(suite_version=None, tasks=0, saturated_share=None, never_solved=[])
    by_task: dict[str, list[float]] = defaultdict(list)
    for s in run.results:
        by_task[s.task_id].append(s.composite)
    means = {t: fmean(v) for t, v in by_task.items()}
    return Saturation(
        suite_version=run.suite_version, tasks=len(means),
        saturated_share=sum(m >= SATURATED_MEAN for m in means.values()) / len(means) if means else None,
        never_solved=sorted(t for t, v in by_task.items() if max(v) == 0),
    )  # fmt: skip


def _coverage(version: str | None, tasks: list[Task] | None) -> Coverage:
    tasks = tasks or []
    tags: dict[str, int] = defaultdict(int)
    for t in tasks:
        for tag in t.tags:
            tags[tag] += 1
    return Coverage(
        suite_version=version, tasks=len(tasks),
        by_language=dict(sorted(_count(t.language for t in tasks).items())),
        by_tier=dict(sorted(_count(t.tier for t in tasks).items())),
        by_tag=dict(sorted(tags.items())), thin_tags=sorted(g for g, n in tags.items() if n < THIN_TAG),
        regression_gate_ready=len(tasks) >= REGRESSION_GATE_TASKS,
    )  # fmt: skip


def _count[K](values: Iterable[K]) -> dict[K, int]:
    out: dict[K, int] = defaultdict(int)
    for v in values:
        out[v] += 1
    return out


def compute(
    runs: list[RunRecord],
    suites: dict[str, list[Task]],
    calibrations: list[CalibrationRecord],
    decisions: list[GateDecision],
    disputes: list[tuple[Dispute, Resolution | None]],
    now: datetime,
    monitor: MonitorReport | None = None,
    open_incidents: int = 0,
) -> EvalHealth:
    runs = sorted(runs, key=lambda r: r.started_at)
    latest = runs[-1] if runs else None
    cost = [
        RunCost(
            run_id=r.run_id, started_at=r.started_at, samples=len(r.results),
            cost_usd=sum(s.cost_usd for s in r.results), solved=(solved := sum(s.composite == 1.0 for s in r.results)),
            cost_per_solved_usd=sum(s.cost_usd for s in r.results) / solved if solved else None,
        )
        for r in runs
    ]  # fmt: skip
    flaky, sat = _flakiness(runs), _saturation(latest)
    cov = _coverage(sat.suite_version, suites.get(sat.suite_version or ""))
    current = passing_versions(calibrations, now)
    by_judge: dict[str, list[CalibrationRecord]] = defaultdict(list)
    for c in sorted(calibrations, key=lambda c: c.created_at):
        by_judge[c.judge_version].append(c)
    judges = [
        JudgeHealth(
            judge_version=v, agreement_within_one=recs[-1].agreement_within_one, passed=recs[-1].passed,
            age_days=(now - recs[-1].created_at).days, current=v in current,
            agreement_change=recs[-1].agreement_within_one - recs[-2].agreement_within_one if len(recs) > 1 else None,
        )
        for v, recs in sorted(by_judge.items())
    ]  # fmt: skip
    blocked = [d for d in decisions if d.outcome == "block"]
    resolved = {d.decision_id: r for d, r in disputes if r is not None}
    overturned = sum(1 for r in resolved.values() if r.outcome != "rejected")
    gates = GateHealth(
        decisions=len(decisions), blocked=len(blocked), disputes_open=sum(r is None for _, r in disputes),
        disputes_resolved=len(resolved), overturned=overturned,
        confirmed_real_rate=1 - overturned / len(blocked) if blocked else None,
    )  # fmt: skip

    issues: list[Issue] = []
    if flaky.rate is not None and flaky.flaky:
        issues.append(Issue(
            severity="critical" if flaky.rate > FLAKY_CRITICAL else "warning", area="flakiness",
            message=f"{flaky.flaky} of {flaky.solutions_regraded} re-graded solutions scored differently across runs "
            f"({flaky.rate:.0%}). Find the nondeterministic task or environment before gating on it.",
        ))  # fmt: skip
    if sat.saturated_share is not None and sat.saturated_share >= SATURATED_SUITE_SHARE:
        issues.append(Issue(
            severity="warning", area="saturation",
            message=f"{sat.saturated_share:.0%} of tasks in {sat.suite_version!r} are solved by nearly every sample, so the "
            "suite can no longer show an improvement. Add harder tasks or retire saturated ones.",
        ))  # fmt: skip
    if sat.never_solved:
        issues.append(Issue(
            severity="warning", area="task quality",
            message=f"{len(sat.never_solved)} tasks were never solved by any sample ({', '.join(sat.never_solved[:5])}). "
            "Check the reference solution and tests before trusting a failure.",
        ))  # fmt: skip
    if latest and not cov.regression_gate_ready:
        issues.append(Issue(
            severity="info", area="coverage",
            message=f"{cov.tasks} tasks; regression gates need at least {REGRESSION_GATE_TASKS}. Hard gates on deterministic graders are unaffected.",
        ))  # fmt: skip
    if cov.thin_tags:
        issues.append(
            Issue(
                severity="info",
                area="coverage",
                message=f"Tags with under {THIN_TAG} tasks: {', '.join(cov.thin_tags[:8])}.",
            )
        )
    for j in judges:
        if not j.passed or j.agreement_within_one < AGREEMENT_TARGET:
            issues.append(
                Issue(
                    severity="critical",
                    area="judge",
                    message=f"Judge {j.judge_version} fails calibration (agreement {j.agreement_within_one:.2f}); it cannot gate.",
                )
            )
        elif not j.current:
            issues.append(
                Issue(
                    severity="critical",
                    area="judge",
                    message=f"Judge {j.judge_version}'s calibration is older than {MAX_AGE.days} days; it can no longer gate. Recalibrate.",
                )
            )
        elif timedelta(days=j.age_days) > STALE_AFTER:
            issues.append(
                Issue(
                    severity="warning",
                    area="judge",
                    message=f"Judge {j.judge_version}'s calibration is {j.age_days} days old; recalibrate before it expires.",
                )
            )
        if j.agreement_change is not None and j.agreement_change < -AGREEMENT_DROP:
            issues.append(
                Issue(
                    severity="warning",
                    area="judge",
                    message=f"Judge {j.judge_version}'s agreement with humans dropped by {-j.agreement_change:.2f} since the previous calibration.",
                )
            )
    used = {s.judge_version for r in runs for s in r.results if s.judge_version}
    if uncal := sorted(used - current):
        issues.append(
            Issue(
                severity="info",
                area="judge",
                message=f"Judge versions {', '.join(uncal)} scored samples without a current passing calibration: advisory only.",
            )
        )
    if gates.confirmed_real_rate is not None and gates.confirmed_real_rate < 0.8:
        issues.append(
            Issue(
                severity="warning",
                area="gates",
                message=f"Only {gates.confirmed_real_rate:.0%} of blocking decisions stood after dispute (target 80%). Check task and grader quality.",
            )
        )
    if gates.disputes_open:
        issues.append(
            Issue(
                severity="info",
                area="gates",
                message=f"{gates.disputes_open} gate disputes are waiting for an approver.",
            )
        )
    if monitor is not None:
        missed = [f for f in monitor.findings if f.kind == "offline_pass_production_fail"]
        if missed:
            issues.append(Issue(
                severity="critical" if any(not f.incident_open for f in missed) else "warning", area="production",
                message=f"{len(missed)} tasks pass offline but fail in production ({', '.join(f.task_id for f in missed[:5])}): "
                "the eval is missing something. Open an incident and add tasks that reproduce it.",
            ))  # fmt: skip
        if monitor.rank_correlation is not None and monitor.rank_correlation < 0.3:
            issues.append(Issue(
                severity="warning", area="production",
                message=f"Offline scores barely predict production outcomes (rank correlation {monitor.rank_correlation:+.2f}).",
            ))  # fmt: skip
    if open_incidents:
        issues.append(
            Issue(severity="info", area="production", message=f"{open_incidents} eval incidents are open.")
        )
    order = {"critical": 0, "warning": 1, "info": 2}
    return EvalHealth(
        generated_at=now, runs=len(runs), flakiness=flaky, saturation=sat, coverage=cov, judges=judges, gates=gates,
        cost=cost, total_cost_usd=sum(c.cost_usd for c in cost), issues=sorted(issues, key=lambda i: order[i.severity]),
    )  # fmt: skip


def render_text(h: EvalHealth) -> str:
    lines = [
        f"Eval health at {h.generated_at:%Y-%m-%d %H:%M} UTC over {h.runs} runs",
        f"  flaky solutions     {h.flakiness.flaky}/{h.flakiness.solutions_regraded} ({_pct(h.flakiness.rate)})",
        f"  saturated tasks     {_pct(h.saturation.saturated_share)} of {h.saturation.tasks} in {h.saturation.suite_version}",
        f"  suite coverage      {h.coverage.tasks} tasks; regression-gate ready: {h.coverage.regression_gate_ready}",
        f"  gate decisions      {h.gates.decisions} ({h.gates.blocked} blocked; {_pct(h.gates.confirmed_real_rate)} stood after dispute)",
        f"  total spend         ${h.total_cost_usd:.2f}",
        "Issues:" if h.issues else "No open issues.",
    ]
    lines += [f"  [{i.severity.upper()}] {i.area}: {i.message}" for i in h.issues]
    return "\n".join(lines)


def render_html(h: EvalHealth) -> str:
    """One self-contained file (no scripts, no network), safe to open from disk or attach to a ticket."""
    e = html.escape
    tiles = [
        ("Flaky solutions", _pct(h.flakiness.rate), f"{h.flakiness.flaky} of {h.flakiness.solutions_regraded} re-graded"),
        ("Saturated tasks", _pct(h.saturation.saturated_share), f"{h.saturation.tasks} tasks in {h.saturation.suite_version}"),
        ("Gate blocks that stood", _pct(h.gates.confirmed_real_rate), f"{h.gates.blocked} blocks, {h.gates.overturned} overturned"),
        ("Total spend", f"${h.total_cost_usd:.2f}", f"{h.runs} runs"),
    ]  # fmt: skip
    tile_html = "".join(
        f"<div class=t><div class=l>{e(a)}</div><div class=v>{e(b)}</div><div class=s>{e(c)}</div></div>"
        for a, b, c in tiles
    )
    issues = (
        "".join(
            f"<li class={i.severity}><b>{e(i.severity)}</b> {e(i.area)}: {e(i.message)}</li>"
            for i in h.issues
        )
        or "<li>No open issues.</li>"
    )
    judges = "".join(
        f"<tr><td>{e(j.judge_version)}</td><td>{j.agreement_within_one:.2f}</td><td>{'yes' if j.current else 'no'}</td><td>{j.age_days}d</td></tr>"
        for j in h.judges
    )
    costs = "".join(
        f"<tr><td>{e(c.run_id[:12])}</td><td>{c.started_at:%Y-%m-%d}</td><td>{c.samples}</td><td>${c.cost_usd:.4f}</td><td>{'n/a' if c.cost_per_solved_usd is None else f'${c.cost_per_solved_usd:.4f}'}</td></tr>"
        for c in h.cost
    )
    cov = "".join(
        f"<tr><td>{e(k)}</td><td>{n}</td></tr>"
        for k, n in {
            **h.coverage.by_language,
            **{f"tag: {k}": n for k, n in h.coverage.by_tag.items()},
        }.items()
    )
    return f"""<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Eval health</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--crit:#b00020;--warn:#9a6700;--info:#0b5cad}}
@media(prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--crit:#ff6b81;--warn:#e3b341;--info:#6cb6ff}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:960px;padding:16px}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}.t{{border:1px solid var(--line);border-radius:8px;padding:12px}}
.l,.s{{color:var(--mut);font-size:13px}}.v{{font-size:28px;font-weight:600}}table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left}}li.critical b{{color:var(--crit)}}li.warning b{{color:var(--warn)}}li.info b{{color:var(--info)}}
h2{{margin-top:28px}}</style>
<h1>Eval health</h1><p class=s>Generated {h.generated_at:%Y-%m-%d %H:%M} UTC</p>
<div class=g>{tile_html}</div><h2>Open issues</h2><ul>{issues}</ul>
<h2>Judges</h2><table><tr><th>Version<th>Agreement<th>Can gate<th>Age</tr>{judges}</table>
<h2>Cost per run</h2><table><tr><th>Run<th>Date<th>Samples<th>Cost<th>Per solved</tr>{costs}</table>
<h2>Coverage ({h.coverage.tasks} tasks)</h2><table>{cov}</table></html>"""
