"""Local CLI: grade one solution file against one task and write a run record (plan §18, local mode)."""

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import yaml

from evalry.agent.remote import RemoteAgent
from evalry.audit.chain import AuditLog
from evalry.billing.cost import Rates, check_budget, estimate
from evalry.gates.hard import GateException, HardGateRule, decide
from evalry.generation.base import FakeGenerator, Generator
from evalry.generation.remote import RemoteGenerator, require_https
from evalry.graders.cache import GradeCache
from evalry.graders.python_tests import DEFAULT_ISOLATION
from evalry.health import EvalHealth, render_html, render_text
from evalry.judge.calibration import Labeled, calibrate
from evalry.judge.remote import RemoteJudge
from evalry.log import get_logger
from evalry.monitoring import MonitorReport
from evalry.monitoring import render_text as render_monitor
from evalry.repo_tasks import Files, RepoError, pack_snapshot, read_snapshot
from evalry.scheduler.engine import grade
from evalry.scheduler.jobs import Job
from evalry.scheduler.suite import load_suite, run_suite, summarize
from evalry.scheduler.worker import sandbox_executor, work
from evalry.schemas import RunRecord, Task, load_task
from evalry.scoring.compare import min_samples, paired_bootstrap, regression_gate, task_means
from evalry.storage.db import MigrationError, migrate
from evalry.storage.objects import objects_from_env
from evalry.storage.pgjobs import PostgresJobQueue
from evalry.storage.pgstore import UnsafeConnection

log = get_logger("cli")
ISOLATION_HELP = (
    "rpc: tests run in a separate container (default, gate-grade). inprocess: same process, advisory only"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evalry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="grade a solution file against a task")
    run.add_argument("task", type=Path)
    run.add_argument("solution", type=Path)
    run.add_argument("--out", type=Path, help="write the run record JSON here")
    run.add_argument("--timeout", type=float, default=10)
    run.add_argument(
        "--isolation", choices=["rpc", "inprocess"], default=DEFAULT_ISOLATION, help=ISOLATION_HELP
    )
    run.add_argument("--suite-version", default="local", help="immutable suite version this task belongs to")
    suite = sub.add_parser("suite", help="run every task in a directory with N samples from recorded replies")
    suite.add_argument("directory", type=Path)
    suite.add_argument(
        "--replies", type=Path, required=True, help='JSON {"task_id": ["reply", ...]}, one reply per sample'
    )
    suite.add_argument("--samples", type=int, default=1)
    suite.add_argument("-k", type=int, default=1, help="k for pass@k")
    suite.add_argument("--suite-version", default="local")
    suite.add_argument("--out", type=Path)
    suite.add_argument("--timeout", type=float, default=10)
    suite.add_argument(
        "--isolation", choices=["rpc", "inprocess"], default=DEFAULT_ISOLATION, help=ISOLATION_HELP
    )
    suite.add_argument(
        "--workers",
        type=int,
        default=1,
        help="jobs in flight at once; each rpc sample runs two containers (~512 MB)",
    )
    suite.add_argument("--project", default="local", help="project id for limits, budget and audit")
    suite.add_argument(
        "--actor", default=getpass.getuser(), help="who is running this, recorded in the audit log"
    )
    suite.add_argument(
        "--audit-log", type=Path, help="append run and job events to this hash-chained audit file"
    )
    suite.add_argument(
        "--cache-dir",
        type=Path,
        help="reuse grading results across runs (keyed by code, tests, grader, image)",
    )
    suite.add_argument(
        "--rates", type=Path, help="JSON of evalry.billing.cost.Rates; prints the estimate before the run"
    )
    suite.add_argument(
        "--budget",
        type=float,
        help="USD hard stop; needs --rates. Refuses to start if the estimate exceeds it",
    )
    suite.add_argument(
        "--expected-tokens-out", type=int, default=500, help="per-sample output tokens for the estimate"
    )
    cmp = sub.add_parser("compare", help="paired-bootstrap comparison of two run records")
    cmp.add_argument("baseline", type=Path)
    cmp.add_argument("candidate", type=Path)
    cmp.add_argument(
        "--threshold", type=float, default=0.05, help="regression gate: block if the CI is below minus this"
    )
    boot = sub.add_parser(
        "bootstrap-tenant",
        help="create a tenant and its first admin (operator action; needs the database admin DSN)",
    )
    boot.add_argument("tenant_id")
    boot.add_argument(
        "admin_subject", help="the identity-provider subject that becomes the tenant's first admin"
    )
    boot.add_argument("--operator", default=getpass.getuser())
    wk = sub.add_parser(
        "worker",
        help="claim queued jobs from the database and grade them; reads EVALRY_DB_WORKER_DSN (the evalry_worker role)",
    )
    wk.add_argument(
        "--generator-url",
        help="generator service base URL (https; token from EVALRY_GEN_TOKEN): the real provider path",
    )
    wk.add_argument(
        "--judge-url",
        help="judge service base URL (https; token from EVALRY_JUDGE_TOKEN); judges solutions that pass every grader",
    )
    wk.add_argument(
        "--judge-weight",
        type=float,
        default=0.0,
        help="weight of the judge score in the composite; 0 keeps it advisory (use >0 only for a calibrated judge)",
    )
    wk.add_argument(
        "--replies",
        type=Path,
        help='JSON {"task_id": ["reply", ...]}; scripted fake generator (offline tests, local runs)',
    )
    wk.add_argument("--workers", type=int, default=1, help="jobs in flight at once in this process")
    wk.add_argument("--timeout", type=float, default=10)
    wk.add_argument(
        "--isolation", choices=["rpc", "inprocess"], default=DEFAULT_ISOLATION, help=ISOLATION_HELP
    )
    wk.add_argument("--once", action="store_true", help="exit when no job is runnable instead of polling")
    wk.add_argument("--poll", type=float, default=2.0, help="seconds to wait when the queue is empty")
    hl = sub.add_parser(
        "health",
        help="fetch a project's eval health from the API and print it or write a self-contained HTML dashboard",
    )
    hl.add_argument(
        "--api", required=True, help="API base URL (https; http only for localhost); token from EVALRY_TOKEN"
    )
    hl.add_argument("--project", required=True)
    hl.add_argument("--html", type=Path, help="write the dashboard here instead of printing the text summary")
    hl.add_argument(
        "--fail-on",
        choices=["critical", "warning"],
        help="exit 1 when an issue of this severity or worse is open",
    )
    mo = sub.add_parser(
        "monitor",
        help="compare a project's offline scores with production outcomes (API; token from EVALRY_TOKEN)",
    )
    mo.add_argument("--api", required=True)
    mo.add_argument("--project", required=True)
    mo.add_argument("--days", type=int, default=30)
    mo.add_argument(
        "--fail-on-finding",
        action="store_true",
        help="exit 1 when offline and production disagree (for cron or CI)",
    )
    snap = sub.add_parser(
        "snapshot",
        help="pack a source tree into a pinned repository snapshot and store it (tier 3 and 4 tasks)",
    )
    snap.add_argument("directory", type=Path)
    snap.add_argument("--tenant", required=True)
    snap.add_argument("--project", required=True)
    cal = sub.add_parser(
        "judge-calibrate",
        help="measure a judge against human labels (plan §6); sends the labeled code to the judge service and spends tokens",
    )
    cal.add_argument("labels", type=Path, help='JSON list of {"prompt", "code", "human_score"} (20+ items)')
    cal.add_argument(
        "--judge-url", required=True, help="judge service base URL; token from EVALRY_JUDGE_TOKEN"
    )
    cal.add_argument(
        "--repeats", type=int, default=2, help="times each item is judged, for the stability check"
    )
    cal.add_argument(
        "--sensitivity", choices=["public", "internal", "confidential", "restricted"], default="internal"
    )
    cal.add_argument(
        "--out", type=Path, help="write the report JSON here; upload it to the judge-calibrations endpoint"
    )
    sub.add_parser(
        "migrate", help="apply database migrations; reads the admin connection string from EVALRY_ADMIN_DSN"
    )
    gate = sub.add_parser(
        "gate", help="hard gate: decide pass/block for a run record and record the decision"
    )
    gate.add_argument("run", type=Path)
    gate.add_argument("--rule", type=Path, required=True, help="YAML of evalry.gates.hard.HardGateRule")
    gate.add_argument("--exceptions", type=Path, help="YAML list of evalry.gates.hard.GateException")
    gate.add_argument("--environment", choices=["dev", "staging", "production"], default="dev")
    gate.add_argument("--project", default="local")
    gate.add_argument("--actor", default=getpass.getuser())
    gate.add_argument("--audit-log", type=Path, required=True)
    gate.add_argument("--out", type=Path, help="write the signed decision JSON here")
    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        return _migrate()
    if args.cmd == "worker":
        return _worker(args)
    if args.cmd == "snapshot":
        return _snapshot(args)
    if args.cmd == "monitor":
        return _monitor(args)
    if args.cmd == "health":
        return _health(args)
    if args.cmd == "judge-calibrate":
        return _judge_calibrate(args)
    if args.cmd == "bootstrap-tenant":
        return _bootstrap(args)
    if args.cmd == "gate":
        return _gate(args)
    if args.cmd == "compare":
        return _compare(args)
    if args.cmd == "suite":
        return _suite(args)

    try:
        result = grade(
            load_task(args.task), args.solution.read_text(), timeout_s=args.timeout, isolation=args.isolation
        )
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    record = RunRecord(
        run_id=uuid.uuid4().hex,
        suite_version=args.suite_version,
        started_at=datetime.now(UTC),
        results=[result],
    )
    if args.out:
        args.out.write_text(record.model_dump_json(indent=2))
    log.info("graded", task_id=result.task_id, composite=result.composite)
    print(f"{result.task_id}: composite={result.composite:.2f}")
    return 0 if result.composite == 1.0 else 1


def _estimate(args: argparse.Namespace, tasks: list[Task]) -> None:
    """Print the pre-run estimate and enforce the budget (plan §15). Input tokens are approximated as chars / 4."""
    if args.budget is not None and args.rates is None:
        raise ValueError("--budget needs --rates to estimate against. Pass a rates JSON file.")
    if args.rates is None:
        return
    rates = Rates.model_validate_json(args.rates.read_text())
    tokens_in = sum(len(t.prompt) for t in tasks) // 4 // max(len(tasks), 1)
    cost = estimate(
        rates,
        tasks=len(tasks),
        samples=args.samples,
        tokens_in=tokens_in,
        tokens_out=args.expected_tokens_out,
    )
    print(f"estimated cost: ${cost:.4f} ({len(tasks)} tasks x {args.samples} samples)")
    if args.budget is not None:
        check_budget(cost, args.budget)


def _suite(args: argparse.Namespace) -> int:
    try:
        replies = json.loads(args.replies.read_text())
        tasks = load_suite(args.directory)
        _estimate(args, tasks)
        cache = GradeCache(args.cache_dir) if args.cache_dir else None
        record = run_suite(
            tasks,
            lambda t, i: FakeGenerator(
                replies.get(t.id, [])[i : i + 1]
            ),  # one scripted reply per (task, sample)
            samples=args.samples,
            suite_version=args.suite_version,
            timeout_s=args.timeout,
            cache=cache,
            isolation=args.isolation,
            workers=args.workers,
            project_id=args.project,
            generator_hash=hashlib.sha256(args.replies.read_bytes()).hexdigest(),
            budget_usd=args.budget if args.budget is not None else float("inf"),
            audit=AuditLog(args.audit_log) if args.audit_log else None,
            actor=args.actor,
        )
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(record.model_dump_json(indent=2))
    if cache:
        print(f"grading cache: {cache.hits} hits, {cache.misses} misses")
    for tid, t in summarize(record, args.k).items():
        p = "n/a" if t.pass_at_k is None else f"{t.pass_at_k:.2f}"
        print(
            f"{tid}: mean={t.stats.mean:.2f} min={t.stats.min:.2f} max={t.stats.max:.2f} std={t.stats.std:.2f} pass@{args.k}={p}"
        )
    return 0


def _worker(args: argparse.Namespace) -> int:
    """Exit 0 on a clean stop, 2 on bad configuration. The DSN holds a password, so it comes from the environment."""
    dsn = os.environ.get("EVALRY_DB_WORKER_DSN")
    if not dsn:
        print("error: set EVALRY_DB_WORKER_DSN to the evalry_worker connection string.", file=sys.stderr)
        return 2
    if bool(args.generator_url) == bool(args.replies):
        print(
            "error: give exactly one of --generator-url (real provider) or --replies (scripted fake).",
            file=sys.stderr,
        )
        return 2
    token = os.environ.get("EVALRY_GEN_TOKEN", "")
    if args.generator_url and not token:
        print("error: set EVALRY_GEN_TOKEN to the generator service token.", file=sys.stderr)
        return 2
    judge_token = os.environ.get("EVALRY_JUDGE_TOKEN", "")
    if args.judge_url and not judge_token:
        print("error: set EVALRY_JUDGE_TOKEN to the judge service token.", file=sys.stderr)
        return 2
    if args.judge_weight < 0:
        print("error: --judge-weight must be 0 or more.", file=sys.stderr)
        return 2
    try:
        replies = json.loads(args.replies.read_text()) if args.replies else {}
        queue = PostgresJobQueue(dsn)
        make_generator: Callable[[Task, int], Generator] = (
            (lambda t, i: RemoteGenerator(args.generator_url, token, sensitivity=t.sensitivity))
            if args.generator_url
            else (lambda t, i: FakeGenerator(replies.get(t.id, [])[i : i + 1]))
        )
    except (OSError, ValueError, psycopg.Error, UnsafeConnection) as e:
        print(
            f"error: {type(e).__name__}: {e}" if isinstance(e, UnsafeConnection) else f"error: {e}",
            file=sys.stderr,
        )
        return 2
    suites: dict[
        tuple[str, str, str], dict[str, Task]
    ] = {}  # suite versions are immutable, so caching them is safe

    def task_for(job: Job) -> Task:
        scope = (job.tenant_id, job.project_id, job.suite_version)
        if scope not in suites:
            suites[scope] = {t.id: t for t in queue.suite_tasks(*scope)}
        return suites[scope][job.task_id]

    objects = objects_from_env(os.environ)
    snapshots: dict[
        str, Files
    ] = {}  # snapshots are pinned by digest, so caching one for the worker's life is safe

    def snapshot_for(job: Job, task: Task) -> Files:
        assert task.repo
        if task.repo.snapshot not in snapshots:
            data = objects.get(job.tenant_id, job.project_id, task.repo.snapshot) if objects else None
            if data is None:
                raise RepoError(
                    f"Snapshot {task.repo.snapshot[:12]}... is not in object storage for this project. Store it with "
                    "evalry snapshot, and give the worker EVALRY_OBJECT_DIR or the EVALRY_S3_* settings."
                )
            snapshots[task.repo.snapshot] = read_snapshot(data, task.repo.snapshot)
        return snapshots[task.repo.snapshot]

    execute = sandbox_executor(
        make_generator,
        timeout_s=args.timeout,
        isolation=args.isolation,
        judge_for=(lambda t: RemoteJudge(args.judge_url, judge_token, sensitivity=t.sensitivity))
        if args.judge_url
        else None,
        judge_weight=args.judge_weight,
        snapshot_for=snapshot_for,
        agent_for=(lambda t: RemoteAgent(args.generator_url, token, sensitivity=t.sensitivity))
        if args.generator_url
        else None,
    )
    try:
        while True:
            done = work(queue, task_for, execute, workers=args.workers)
            if args.once:
                return 0
            if done == 0:
                time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0
    finally:
        queue.close()


def _snapshot(args: argparse.Namespace) -> int:
    """Pack deterministically, validate, store by digest. Prints the digest a task's ``repo.snapshot`` pins."""
    try:
        objects = objects_from_env(os.environ)
        if objects is None:
            print(
                "error: set EVALRY_OBJECT_DIR (dev) or the EVALRY_S3_* settings to say where snapshots are stored.",
                file=sys.stderr,
            )
            return 2
        data = pack_snapshot(args.directory)
        read_snapshot(
            data
        )  # the same validation a worker applies, so a bad snapshot fails here and not mid-run
        stored = objects.put(args.tenant, args.project, data)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{stored}  ({len(data)} bytes)")
    return 0


def _monitor(args: argparse.Namespace) -> int:
    """Exit 0 = rendered, 1 = findings with --fail-on-finding, 2 = could not fetch."""
    token = os.environ.get("EVALRY_TOKEN", "")
    if not token:
        print("error: set EVALRY_TOKEN to an access token for the API.", file=sys.stderr)
        return 2
    try:
        require_https(args.api)
        req = urllib.request.Request(
            f"{args.api.rstrip('/')}/v1/projects/{args.project}/production-monitor?days={args.days}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            report = MonitorReport.model_validate_json(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error: the API returned {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(render_monitor(report))
    return int(args.fail_on_finding and bool(report.findings))


def _health(args: argparse.Namespace) -> int:
    """Exit 0 = rendered (and no issue at --fail-on), 1 = issue at or above --fail-on, 2 = could not fetch."""
    token = os.environ.get("EVALRY_TOKEN", "")
    if not token:
        print("error: set EVALRY_TOKEN to an access token for the API.", file=sys.stderr)
        return 2
    try:
        require_https(args.api)
        req = urllib.request.Request(
            f"{args.api.rstrip('/')}/v1/projects/{args.project}/eval-health",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            report = EvalHealth.model_validate_json(resp.read())
    except urllib.error.HTTPError as e:
        print(f"error: the API returned {e.code}: {e.read().decode(errors='replace')[:300]}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.html:
        args.html.write_text(render_html(report))
        print(f"wrote {args.html}")
    else:
        print(render_text(report))
    rank = {"critical": 0, "warning": 1}
    return int(
        bool(args.fail_on)
        and any(i.severity in rank and rank[i.severity] <= rank[args.fail_on] for i in report.issues)
    )


def _judge_calibrate(args: argparse.Namespace) -> int:
    """Exit 0 = every target met, 1 = judge not calibrated, 2 = bad input. Never auto-records: an approver uploads the report."""
    token = os.environ.get("EVALRY_JUDGE_TOKEN", "")
    if not token:
        print("error: set EVALRY_JUDGE_TOKEN to the judge service token.", file=sys.stderr)
        return 2
    try:
        labeled = [Labeled.model_validate(x) for x in json.loads(args.labels.read_text())]
        report = calibrate(
            RemoteJudge(args.judge_url, token, sensitivity=args.sensitivity), labeled, repeats=args.repeats
        )
    except (OSError, ValueError, RuntimeError, TypeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(report.model_dump_json(indent=2))
    print(
        f"judge {report.judge_version}: agreement within one point {report.agreement_within_one:.2f} (need 0.75), "
        f"stability {report.stability:.2f} (need 0.90), injection-resistant {report.injection_resistant} "
        f"over {report.items} items: {'PASS' if report.passed else 'FAIL'}"
    )
    return 0 if report.passed else 1


def _migrate() -> int:
    """Exit 0 = up to date, 2 = cannot migrate. The DSN holds a password, so it comes from the environment, not a flag."""
    dsn = os.environ.get("EVALRY_ADMIN_DSN")
    if not dsn:
        print("error: set EVALRY_ADMIN_DSN to the schema owner's connection string.", file=sys.stderr)
        return 2
    try:
        applied = migrate(dsn)
    except (MigrationError, psycopg.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("applied: " + ", ".join(applied) if applied else "database is up to date")
    return 0


def _bootstrap(args: argparse.Namespace) -> int:
    """Exit 0 = created, 2 = refused. Uses the same environment variables as the server."""
    from evalry.access.store import Conflict
    from evalry.api.server import ConfigError, bootstrap_tenant, load_settings

    try:
        bootstrap_tenant(load_settings(os.environ), args.tenant_id, args.admin_subject, args.operator)
    except (ConfigError, Conflict, psycopg.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"tenant {args.tenant_id!r} created with admin {args.admin_subject!r}")
    return 0


def _gate(args: argparse.Namespace) -> int:
    """Exit 0 = pass, 1 = block, 2 = bad input. The signing key comes from EVALRY_GATE_KEY, never from a flag."""
    try:
        record = RunRecord.model_validate_json(args.run.read_text())
        rule = HardGateRule.model_validate(yaml.safe_load(args.rule.read_text()))
        raw = yaml.safe_load(args.exceptions.read_text()) if args.exceptions else []
        exceptions = [GateException.model_validate(e) for e in raw]
        key = os.environ.get("EVALRY_GATE_KEY", "").encode() or None
        decision = decide(
            record, rule, actor=args.actor, environment=args.environment, project_id=args.project,
            key=key, audit=AuditLog(args.audit_log), exceptions=exceptions,
        )  # fmt: skip
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(decision.model_dump_json(indent=2))
    print(
        f"gate {decision.gate}: {decision.outcome.upper()} ({decision.environment}, signed={bool(decision.signature)})"
    )
    for reason in decision.reasons:
        print(f"  - {reason}")
    return 0 if decision.outcome == "pass" else 1


def _compare(args: argparse.Namespace) -> int:
    """Exit 0 = no regression (or underpowered, reported as advisory), 1 = gate blocks, 2 = bad input."""
    try:
        base = RunRecord.model_validate_json(args.baseline.read_text())
        cand = RunRecord.model_validate_json(args.candidate.read_text())
        if base.suite_version != cand.suite_version:
            raise ValueError(
                f"Suite versions differ ({base.suite_version!r} vs {cand.suite_version!r}). "
                "Compare runs of the same immutable suite version."
            )
        delta = paired_bootstrap(task_means(base), task_means(cand))
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(
        f"delta={delta.mean:+.3f} 95% CI [{delta.lo:+.3f}, {delta.hi:+.3f}] over {len(task_means(base).keys() & task_means(cand).keys())} tasks"
    )
    try:
        blocked = regression_gate(
            delta,
            args.threshold,
            n_tasks=len(task_means(cand)),
            min_samples=min(min_samples(base), min_samples(cand)),
        )
    except ValueError as e:
        print(f"advisory only: {e}")
        return 0
    print("REGRESSION: gate blocks" if blocked else "no regression beyond the threshold")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
