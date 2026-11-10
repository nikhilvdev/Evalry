"""Local CLI: grade one solution file against one task and write a run record (plan §18, local mode)."""

import argparse
import getpass
import hashlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from evalry.audit import AuditLog
from evalry.cache import GradeCache
from evalry.compare import min_samples, paired_bootstrap, regression_gate, task_means
from evalry.cost import Rates, check_budget, estimate
from evalry.engine import grade
from evalry.gates import GateException, HardGateRule, decide
from evalry.generation import FakeGenerator
from evalry.graders import DEFAULT_ISOLATION
from evalry.log import get_logger
from evalry.schemas import RunRecord, Task, load_task
from evalry.suite import load_suite, run_suite, summarize

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
        "--rates", type=Path, help="JSON of evalry.cost.Rates; prints the estimate before the run"
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
    gate = sub.add_parser(
        "gate", help="hard gate: decide pass/block for a run record and record the decision"
    )
    gate.add_argument("run", type=Path)
    gate.add_argument("--rule", type=Path, required=True, help="YAML of evalry.gates.HardGateRule")
    gate.add_argument("--exceptions", type=Path, help="YAML list of evalry.gates.GateException")
    gate.add_argument("--environment", choices=["dev", "staging", "production"], default="dev")
    gate.add_argument("--project", default="local")
    gate.add_argument("--actor", default=getpass.getuser())
    gate.add_argument("--audit-log", type=Path, required=True)
    gate.add_argument("--out", type=Path, help="write the signed decision JSON here")
    args = ap.parse_args(argv)
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
