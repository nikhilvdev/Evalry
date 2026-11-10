"""Local CLI: grade one solution file against one task and write a run record (plan §18, local mode)."""

import argparse
import hashlib
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from evalry.compare import min_samples, paired_bootstrap, regression_gate, task_means
from evalry.generation import FakeGenerator, Generator, extract_code, generate_logged
from evalry.graders import python_tests
from evalry.log import get_logger
from evalry.runner import PYTHON_IMAGE, SEED
from evalry.schemas import RunRecord, SampleResult, Task, load_task
from evalry.scoring import composite

log = get_logger("cli")


def grade(
    task: Task,
    code: str,
    *,
    timeout_s: float = 10,
    generator_model: str = "local-file",
    sample: int = 0,
    cost_usd: float = 0,
) -> SampleResult:
    """Grade one solution. Only Python tasks have a grader so far."""
    if task.language != "python":
        raise ValueError(
            f"Task {task.id!r} is {task.language}; only python is supported. Add a language profile first."
        )
    score, hard_fail = python_tests(task, code, timeout_s=timeout_s)
    return SampleResult(
        task_id=task.id,
        task_hash=hashlib.sha256(task.model_dump_json().encode()).hexdigest(),
        sample=sample,
        generator_model=generator_model,
        code=code,
        scores=[score],
        composite=composite([score], {}, hard_fail=hard_fail),
        cost_usd=cost_usd,
        image=PYTHON_IMAGE,
        seed=SEED,
    )


def run_sample(task: Task, generator: Generator, sample: int, *, timeout_s: float = 10) -> SampleResult:
    """Generate (prompt only, never the tests), extract the code, grade it."""
    out = generate_logged(generator, task.prompt, sample=sample)
    return grade(
        task,
        extract_code(out.text),
        timeout_s=timeout_s,
        generator_model=out.model,
        sample=sample,
        cost_usd=out.cost_usd,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evalry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="grade a solution file against a task")
    run.add_argument("task", type=Path)
    run.add_argument("solution", type=Path)
    run.add_argument("--out", type=Path, help="write the run record JSON here")
    run.add_argument("--timeout", type=float, default=10)
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
    cmp = sub.add_parser("compare", help="paired-bootstrap comparison of two run records")
    cmp.add_argument("baseline", type=Path)
    cmp.add_argument("candidate", type=Path)
    cmp.add_argument(
        "--threshold", type=float, default=0.05, help="regression gate: block if the CI is below minus this"
    )
    args = ap.parse_args(argv)
    if args.cmd == "compare":
        return _compare(args)
    if args.cmd == "suite":
        return _suite(args)

    try:
        result = grade(load_task(args.task), args.solution.read_text(), timeout_s=args.timeout)
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


def _suite(args: argparse.Namespace) -> int:
    from evalry.suite import load_suite, run_suite, summarize  # local: suite imports this module

    try:
        replies = json.loads(args.replies.read_text())
        record = run_suite(
            load_suite(args.directory),
            lambda t: FakeGenerator(replies.get(t.id, [])),
            samples=args.samples,
            suite_version=args.suite_version,
            timeout_s=args.timeout,
        )
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.out:
        args.out.write_text(record.model_dump_json(indent=2))
    for tid, t in summarize(record, args.k).items():
        p = "n/a" if t.pass_at_k is None else f"{t.pass_at_k:.2f}"
        print(
            f"{tid}: mean={t.stats.mean:.2f} min={t.stats.min:.2f} max={t.stats.max:.2f} std={t.stats.std:.2f} pass@{args.k}={p}"
        )
    return 0


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
