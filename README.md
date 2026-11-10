# Evalry

A shared platform for evaluating AI-generated code, the datasets that feed and judge it, and the evals themselves, with
isolation, audit trails, access control and cost governance.

> **Status: early development.** A local, single-user engine works: grade solutions in a Docker sandbox, run suites with N
> samples, compare runs. The API, scheduler, storage, auth, real model providers and judges are not built yet.

## What exists

| Module | Purpose |
| --- | --- |
| `evalry.schemas` | Version 1 public schemas: task YAML (`Task`, `load_task`) and run record JSON (`RunRecord`, `SampleResult`, `GraderScore`) |
| `evalry.runner` | Docker sandbox for untrusted code: pinned image, no network, non-root, read-only root, capped CPU/memory/processes |
| `evalry.graders` | `python_tests`: hidden tests run in the sandbox |
| `evalry.generation` | `Generator` interface, offline `FakeGenerator`, `extract_code`, logged generation calls |
| `evalry.engine` | `grade` and `run_sample`: the steps shared by the CLI and suite runs |
| `evalry.cache` | Grading cache keyed by code, tests, grader version, image and seed (`--cache-dir`); timeouts are never cached |
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
uv run evalry run task.yaml solution.py [--out run.json] [--suite-version V] [--timeout S]

# Run a directory of task YAML files, N samples each, from recorded model replies.
# replies.json: {"task_id": ["reply for sample 0", "reply for sample 1", ...]}
uv run evalry suite tasks/ --replies replies.json --samples 5 -k 1 --out run.json

# Compare two run records of the same suite version (paired bootstrap, 95% interval).
# Exit code: 0 no regression or too small to gate, 1 regression gate blocks, 2 bad input.
uv run evalry compare baseline.json candidate.json [--threshold 0.05]
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

- The sandbox shares the host kernel. Hidden tests are kept off disk and out of reach during import, but code that survives
  import can still reach them in-process; a stronger isolation option is planned.
- Suite runs are sequential and not resumable; there is no generation or grading cache yet.
- Local comparisons accept any run as the baseline; approved baselines come with the approvals workflow.

## Releases

Tag `v<version>` matching `pyproject.toml`, `evalry.__version__` and a `CHANGELOG.md` heading; the release workflow
checks all three before publishing.
