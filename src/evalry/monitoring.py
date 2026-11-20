"""Production monitoring loop (plan §11 "Closing the loop with production").

Production systems push masked outcome signals about generated code (accepted, edited, rejected, build failed, reverted), keyed
by the id of the suite task that stands for that scenario. We compare production outcomes with offline scores task by task:
a task that passes offline but fails in production means the eval is missing something, so the finding becomes an eval incident
that is closed by adding tasks that reproduce it. Signals carry no code or user content by schema (``extra="forbid"``), which
is how masking is enforced. Thresholds are this module's choice; tune them with the eval owner. Nothing here samples real code.
"""

from collections import defaultdict
from datetime import datetime
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SignalKind = Literal["accepted", "edited", "rejected", "build_failed", "reverted"]
# Production score per signal: an edited suggestion is half a success, anything that failed or was undone is none.
SCORE: dict[str, float] = {
    "accepted": 1.0,
    "edited": 0.5,
    "rejected": 0.0,
    "build_failed": 0.0,
    "reverted": 0.0,
}
MIN_SIGNALS = 20  # below this a task's production score is noise
OFFLINE_HIGH, PRODUCTION_LOW = 0.9, 0.5
OFFLINE_LOW, PRODUCTION_HIGH = 0.3, 0.8
MIN_TASKS_FOR_CORRELATION = 5


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Signal(_Strict):
    signal_id: str = Field(
        min_length=1, max_length=128, description="Caller-chosen id; resending it is harmless."
    )
    task_id: str = Field(min_length=1, max_length=256)
    kind: SignalKind
    observed_at: datetime
    generator_model: str | None = Field(default=None, max_length=128)


class Finding(_Strict):
    task_id: str
    kind: Literal["offline_pass_production_fail", "offline_fail_production_pass"]
    offline: float
    production: float
    signals: int
    incident_open: bool


class TaskComparison(_Strict):
    task_id: str
    offline: float | None
    production: float | None
    signals: int


class MonitorReport(_Strict):
    generated_at: datetime
    window_days: int
    run_id: str | None
    signals: int
    unmatched_signals: int
    tasks_compared: int
    rank_correlation: float | None = Field(
        description="Spearman correlation of offline and production scores across tasks."
    )
    findings: list[Finding]
    tasks: list[TaskComparison]


class Incident(_Strict):
    incident_id: str
    task_id: str
    kind: Literal["offline_pass_production_fail", "offline_fail_production_pass", "other"]
    note: str = Field(min_length=1, max_length=10_000)
    opened_by: str
    created_at: datetime


class ClosureIn(_Strict):
    outcome: Literal["tasks_added", "not_an_eval_problem"]
    rationale: str = Field(min_length=1, max_length=10_000)
    suite_version: str | None = Field(
        default=None, description="The suite version that holds the added tasks."
    )
    added_task_ids: list[str] = []


class Closure(_Strict):
    incident_id: str
    outcome: Literal["tasks_added", "not_an_eval_problem"]
    rationale: str
    suite_version: str | None
    added_task_ids: list[str]
    closed_by: str
    created_at: datetime


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1  # ties share the average rank
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation, or None when it is undefined (too few points, or one side is constant)."""
    if len(a) < 2:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = fmean(ra), fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return None if den == 0 else num / den


def compare(
    offline: dict[str, float],
    signals: list[Signal],
    *,
    run_id: str | None,
    window_days: int,
    open_incident_tasks: set[str],
    now: datetime,
) -> MonitorReport:
    """``offline`` is each task's mean composite in the latest run. Signals for tasks outside it are counted, not scored."""
    by_task: dict[str, list[float]] = defaultdict(list)
    unmatched = 0
    for s in signals:
        if s.task_id in offline:
            by_task[s.task_id].append(SCORE[s.kind])
        else:
            unmatched += 1
    rows = [
        TaskComparison(task_id=t, offline=offline.get(t), production=fmean(by_task[t]) if by_task.get(t) else None, signals=len(by_task.get(t, [])))
        for t in sorted(offline)
    ]  # fmt: skip
    scored = [
        (r.task_id, r.offline, r.production, r.signals)
        for r in rows
        if r.signals >= MIN_SIGNALS and r.offline is not None and r.production is not None
    ]
    findings: list[Finding] = []
    for task_id, off, prod, n in scored:
        kind: Literal["offline_pass_production_fail", "offline_fail_production_pass"]
        if off >= OFFLINE_HIGH and prod <= PRODUCTION_LOW:
            kind = "offline_pass_production_fail"
        elif off <= OFFLINE_LOW and prod >= PRODUCTION_HIGH:
            kind = "offline_fail_production_pass"
        else:
            continue
        findings.append(
            Finding(
                task_id=task_id,
                kind=kind,
                offline=off,
                production=prod,
                signals=n,
                incident_open=task_id in open_incident_tasks,
            )
        )
    corr = (
        spearman([x[1] for x in scored], [x[2] for x in scored])
        if len(scored) >= MIN_TASKS_FOR_CORRELATION
        else None
    )
    return MonitorReport(
        generated_at=now, window_days=window_days, run_id=run_id, signals=len(signals), unmatched_signals=unmatched,
        tasks_compared=len(scored), rank_correlation=corr,
        findings=sorted(findings, key=lambda f: (f.kind, -f.signals)), tasks=rows,
    )  # fmt: skip


def render_text(r: MonitorReport) -> str:
    corr = "n/a" if r.rank_correlation is None else f"{r.rank_correlation:+.2f}"
    lines = [
        f"Production monitor: {r.signals} signals over {r.window_days} days against run {r.run_id}",
        f"  tasks compared {r.tasks_compared} (need {MIN_SIGNALS}+ signals each); offline vs production rank correlation {corr}",
        f"  signals for tasks outside the suite: {r.unmatched_signals}",
        "Findings:" if r.findings else "No disagreement between offline scores and production.",
    ]
    for f in r.findings:
        state = "incident open" if f.incident_open else "NO INCIDENT"
        lines.append(
            f"  {f.task_id}: {f.kind.replace('_', ' ')} (offline {f.offline:.2f}, production {f.production:.2f}, n={f.signals}) [{state}]"
        )
    return "\n".join(lines)
