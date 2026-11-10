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
| `evalry.access` | Roles (viewer/author/approver/admin), tenant and project scoping in one data-access layer; cross-tenant access is structurally impossible and every denial, grant and write is audited |
| `evalry.service` | Request-level operations behind roles: `submit_suite` (author) and `gate_run` (approver; every exception's named approver must hold the approver role). The local CLI does not go through it |
| `evalry.api` | FastAPI app under `/v1`: OIDC bearer-token verification (RS256/ES256 only), project and member management, run import and read, gate and decision endpoints. State is injected; environment and the gate key are server config |
| `evalry.backend`, `evalry.pgstore`, `evalry.db` | Storage behind the access layer: in-memory or PostgreSQL (tenant in every key, row-level security, immutable runs and decisions via triggers), plus plain-SQL migrations with checksums |
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
#   --workers N            run N jobs at once (a project's concurrency limit still applies); ~2x faster at 4 workers on a laptop
#   --rates R.json --budget USD   print the estimate first; refuse to start (and stop mid-run) over budget
#   --project P --actor A --audit-log audit.jsonl   record run and job events in the audit log

# Compare two run records of the same suite version (paired bootstrap, 95% interval).
# Exit code: 0 no regression or too small to gate, 1 regression gate blocks, 2 bad input.
uv run evalry compare baseline.json candidate.json [--threshold 0.05]

# Apply database migrations (connection string from the environment, never a flag).
EVALRY_ADMIN_DSN='host=... user=... password=... dbname=evalry' uv run evalry migrate

# Hard gate: block a release when correctness falls below the floor. Exit code: 0 pass, 1 block, 2 bad input.
# Rule YAML: name, suite_version (the approved one, never "latest"), grader_version, [floor, min_pass_rate, required_tasks].
# Production decisions must be signed with the key in EVALRY_GATE_KEY; dev and staging record rehearsals.
# Only `rpc` isolation results can gate. Exceptions: YAML list of task_id, approver, reason, expires_at.
uv run evalry gate run.json --rule gate.yaml --audit-log audit.jsonl [--exceptions exc.yaml] [--environment production] [--out decision.json]
```

Regression gates need at least 100 tasks and 5 samples per task. Smaller runs print the delta and interval as advisory only.
Run records store the task hash, grader versions, runner image digest and seed so a score can be reproduced.

## Local Postgres

```
cp deploy/.env.example deploy/.env   # set both passwords; never commit deploy/.env
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --wait
```

The compose file creates the least-privilege `evalry_app` role (not a superuser, no `BYPASSRLS`). Run `evalry migrate` as the
admin, then connect the application as `evalry_app`; `PostgresBackend` refuses a superuser connection because PostgreSQL
superusers bypass row-level security.

## Run the API

```
# one-time: schema, then the first tenant (operator actions; they use the admin connection)
export EVALRY_ENV=staging EVALRY_DB_APP_DSN='host=... user=evalry_app password=... dbname=evalry'
export EVALRY_DB_ADMIN_DSN='host=... user=postgres password=... dbname=evalry'
EVALRY_ADMIN_DSN="$EVALRY_DB_ADMIN_DSN" uv run evalry migrate
export EVALRY_OIDC_ISSUER=https://login.example.com EVALRY_OIDC_AUDIENCE=evalry-api \
       EVALRY_OIDC_JWKS_URL=https://login.example.com/.well-known/jwks.json
uv run evalry bootstrap-tenant acme alice          # alice = the identity-provider subject of the first admin

uv run evalry-api                                  # binds 127.0.0.1:8000 unless EVALRY_HOST / EVALRY_PORT say otherwise
```

| Variable | Meaning |
| --- | --- |
| `EVALRY_ENV` | `dev`, `staging` or `production` (required). Only `production` records official gate decisions |
| `EVALRY_OIDC_ISSUER`, `EVALRY_OIDC_AUDIENCE`, `EVALRY_OIDC_JWKS_URL` | Token verification (RS256/ES256 only); the JWKS URL must be https. `EVALRY_TENANT_CLAIM` names the tenant claim (default `tenant_id`) |
| `EVALRY_DB_APP_DSN` | PostgreSQL connection as the least-privilege `evalry_app` role. Required in staging and production |
| `EVALRY_DB_ADMIN_DSN` | Admin connection, used only by `bootstrap-tenant` |
| `EVALRY_GATE_KEY` | Signing key for gate decisions. Required in production |
| dev only | `EVALRY_OIDC_PUBLIC_KEY_FILE` (a PEM instead of a JWKS URL), `EVALRY_DEV_TENANTS=acme:alice,...`, `EVALRY_AUDIT_FILE`. Without a database dev uses the in-memory store and a local audit file |

Every configuration problem is reported at once, and secret values are never printed. Audit events go to one hash chain per
tenant in the database; a database outage returns 503 without echoing the connection string.

## Develop

```
uv sync
uv run ruff format . && uv run ruff check .
uv run mypy src tests
uv run pytest            # sandbox and Postgres tests need Docker and skip without it (Postgres tests start a throwaway container)
```

- Log only through `evalry.log.get_logger(component)`; never `print` or bare `logging` in library code.
- A score change fails `tests/golden`. Update `tests/golden/expected.json` only when the change is intended.
- Tests never call a real model: use `FakeGenerator`.

## Known limits

- The sandbox shares the host kernel; a stronger isolation option (gVisor or microVMs) is planned.
- `rpc` isolation (default) keeps the hidden tests out of the solution's reach, but the solution sees the test *inputs*, and
  tests that need classes, mutated arguments, callbacks or generators fail closed (score 0, exit 4). Use `--isolation inprocess`
  for those; its results are advisory and must not gate. `rpc` runs two containers per sample: about 0.2 s warm (1.3 s for the
  first, cold call), measured on a laptop.
- Suite runs can use `--workers N` threads but are not resumable across process restarts; there is no generation cache yet.
- Budget stops are checked when a job is claimed, so parallel jobs already running can overshoot by up to the number of workers.
- Running suites through the API waits for the generation service; the API stores, reads and gates run records. `evalry.access` and `evalry.service` are in-memory and not yet behind an API or SSO: roles are assigned in the store, not read from an identity provider,
  and reads are not audited (denials, grants and writes are).
- Local comparisons accept any run as the baseline; approved baselines come with the approvals workflow.

## Releases

Tag `v<version>` matching `pyproject.toml`, `evalry.__version__` and a `CHANGELOG.md` heading; the release workflow
checks all three before publishing.
