# Evalry

A shared platform for evaluating AI-generated code, the datasets that feed and judge it, and the evals themselves, with
isolation, audit trails, access control and cost governance.

> **Status: early development.** A local, single-user engine works: grade solutions in a Docker sandbox, run suites with N
> samples, compare runs. The API, scheduler, storage, auth, real model providers and judges are not built yet.

## What exists

| Module | Purpose |
| --- | --- |
| `evalry.schemas` | Version 1 public schemas: task YAML (`Task`, `load_task`) and run record JSON (`RunRecord`, `SampleResult`, `GraderScore`) |
| `evalry.runner` | Docker sandbox for untrusted code: pinned image, no network, non-root, read-only root, capped CPU/memory/processes. Default `rpc` isolation runs the solution and the hidden tests in separate containers joined by a host relay |
| `evalry.graders` | `python_tests`: hidden tests run in the sandbox |
| `evalry.generation` | `Generator` interface, offline `FakeGenerator`, `extract_code`, logged generation calls |
| `evalry.engine` | `grade` and `run_sample`: the steps shared by the CLI and suite runs |
| `evalry.cache` | Grading cache keyed by code, tests, grader version, image and seed (`--cache-dir`); timeouts are never cached |
| `evalry.jobs` | Idempotent jobs keyed by their inputs, in-process priority queue, per-project concurrency and budget limits, retries, crash recovery |
| `evalry.worker` | `submit_run`, `work`, `collect`: the loop that runs queued jobs through the engine |
| `evalry.audit` | Append-only, hash-chained audit log (`AuditLog.verify` detects edits, removals and, with a stored head, truncation) |
| `evalry.gates` | Hard gate: pass/block on deterministic graders for an approved suite version, exceptions with a named approver and expiry, signed decisions recorded in the audit log |
| `evalry.suite` | Run every task x N samples; per-task mean/min/max/std and `pass@k` |
| `evalry.scoring` | Composite score with hard floors, unbiased `pass_at_k`, per-task stats |
| `evalry.compare` | Seeded paired bootstrap over tasks and the regression gate |
| `evalry.cost` | Pre-run cost estimate and budget stop; rates are inputs |
| `evalry.log` | `get_logger`, the only way to log: [LogQuill](https://pypi.org/project/logquill/) with redaction attached |

## Requirements

Python 3.12+, [uv](https://docs.astral.sh/uv/), and a running Docker daemon (untrusted code only ever runs in a container).

## Task format

```yaml
id: fizzbuzz
language: python
prompt: Write fizzbuzz(n).
tests: assert fizzbuzz(3) == 'Fizz'   # hidden from generators
```

Unknown fields are rejected. Optional fields: `tier` (1 to 4), `reference`, `tags`, `sensitivity`, `owner`. Only Python tasks
can be graded so far.

## Command line

```
# Grade one solution file. Exit code: 0 perfect score, 1 lower score, 2 usage or file error.
uv run evalry run task.yaml solution.py [--out run.json] [--suite-version V] [--timeout S] [--isolation rpc|inprocess]

# Run a directory of task YAML files, N samples each, from recorded model replies.
# replies.json: {"task_id": ["reply for sample 0", "reply for sample 1", ...]}
uv run evalry suite tasks/ --replies replies.json --samples 5 -k 1 --out run.json
#   --cache-dir DIR        reuse grades across runs
#   --rates R.json --budget USD   print the estimate first; refuse to start (and stop mid-run) over budget
#   --project P --actor A --audit-log audit.jsonl   record run and job events in the audit log

# Compare two run records of the same suite version (paired bootstrap, 95% interval).
# Exit code: 0 no regression or too small to gate, 1 regression gate blocks, 2 bad input.
uv run evalry compare baseline.json candidate.json [--threshold 0.05]

# Hard gate: block a release when correctness falls below the floor. Exit code: 0 pass, 1 block, 2 bad input.
# Rule YAML: name, suite_version (the approved one, never "latest"), grader_version, [floor, min_pass_rate, required_tasks].
# Production decisions must be signed with the key in EVALRY_GATE_KEY; dev and staging record rehearsals.
# Only `rpc` isolation results can gate. Exceptions: YAML list of task_id, approver, reason, expires_at.
uv run evalry gate run.json --rule gate.yaml --audit-log audit.jsonl [--exceptions exc.yaml] [--environment production] [--out decision.json]
```

Regression gates need at least 100 tasks and 5 samples per task. Smaller runs print the delta and interval as advisory only.
Run records store the task hash, grader versions, runner image digest and seed so a score can be reproduced.

## Develop

```
uv sync
uv run ruff format . && uv run ruff check .
uv run mypy src tests
uv run pytest            # sandbox tests need Docker and skip without it
```

- Log only through `evalry.log.get_logger(component)`; never `print` or bare `logging` in library code.
- A score change fails `tests/golden`. Update `tests/golden/expected.json` only when the change is intended.
- Tests never call a real model: use `FakeGenerator`.

## Known limits

- The sandbox shares the host kernel; a stronger isolation option (gVisor or microVMs) is planned.
- `rpc` isolation (default) keeps the hidden tests out of the solution's reach, but the solution sees the test *inputs*, and
  tests that need classes, mutated arguments, callbacks or generators fail closed (score 0, exit 4). Use `--isolation inprocess`
  for those; its results are advisory and must not gate. It costs about 1.3 s per sample (two containers).
- Suite runs are sequential and not resumable; there is no generation or grading cache yet.
- Local comparisons accept any run as the baseline; approved baselines come with the approvals workflow.

## Releases

Tag `v<version>` matching `pyproject.toml`, `evalry.__version__` and a `CHANGELOG.md` heading; the release workflow
checks all three before publishing.
