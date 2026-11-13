# Using Evalry

## 1. Grade locally (no server)

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and a running Docker daemon.

```
uv sync
```

Write a task (`task.yaml`) and a solution (`solution.py`):

```yaml
id: double
language: python
prompt: Write double(n) returning n * 2.
tests: |
  assert double(2) == 4
  assert double(0) == 0
```

```
uv run evalry run task.yaml solution.py --out run.json      # exit 0 = perfect, 1 = lower, 2 = bad input
```

For many tasks and samples, put one task YAML per file in a directory and recorded model replies in a JSON file
(`{"task_id": ["reply for sample 0", ...]}`; replies may contain fenced code, the last block is used):

```
uv run evalry suite tasks/ --replies replies.json --samples 5 -k 1 --suite-version v1 --out run.json --workers 4
```

It prints per-task mean, min, max, std and pass@k. Useful flags: `--cache-dir` reuses grades, `--rates r.json --budget 5`
prints an estimate first and refuses to start over budget, `--audit-log audit.jsonl` records events.

Compare two runs of the same suite version:

```
uv run evalry compare baseline.json candidate.json --threshold 0.05   # exit 1 = regression
```

A regression only blocks with at least 100 tasks and 5 samples per task; smaller runs are advisory.

## 2. Gate a release in CI

Write a rule (`gate.yaml`) naming the approved suite version:

```yaml
name: smoke
suite_version: v1
grader_version: "2"
min_pass_rate: 1.0
required_tasks: [double]
```

```
uv run evalry gate run.json --rule gate.yaml --audit-log audit.jsonl --environment staging
```

Exit 0 passes, 1 blocks, 2 is bad input. Only results graded with `rpc` isolation count. `staging` and `dev` record a
rehearsal; `production` needs `EVALRY_GATE_KEY` and records the official signed decision.

To waive one failing task, pass `--exceptions exc.yaml`, a list of `task_id`, `approver`, `reason`, `expires_at`. The approver is
named in the audit log and the waiver expires.

The repository ships `.github/workflows/eval-gate.yml`: it runs a suite, then the gate. Pull requests record a staging
rehearsal; pushes to `main` record the production decision using the `EVALRY_GATE_KEY` repository secret. Other repositories can
call it with `workflow_call`, passing `suite-dir`, `replies`, `rule`, `suite-version` and `project`.

## 3. Run the server

```
cp deploy/.env.example deploy/.env      # set every password and key; never commit it
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --wait   # Postgres and MinIO

export EVALRY_ENV=staging
export EVALRY_DB_APP_DSN='host=... user=evalry_app password=... dbname=evalry'
export EVALRY_DB_ADMIN_DSN='host=... user=postgres password=... dbname=evalry'
EVALRY_ADMIN_DSN="$EVALRY_DB_ADMIN_DSN" uv run evalry migrate
export EVALRY_OIDC_ISSUER=https://login.example.com EVALRY_OIDC_AUDIENCE=evalry-api \
       EVALRY_OIDC_JWKS_URL=https://login.example.com/.well-known/jwks.json
export EVALRY_S3_BUCKET=evalry EVALRY_S3_ENDPOINT=http://127.0.0.1:9000 \
       EVALRY_S3_ACCESS_KEY=... EVALRY_S3_SECRET_KEY=...        # create the bucket first
uv run evalry bootstrap-tenant acme alice      # alice = the identity provider subject of the first admin
uv run evalry-api
```

Configuration problems are all reported at once, and secrets are never printed. For local trials use `EVALRY_ENV=dev` with
`EVALRY_OIDC_PUBLIC_KEY_FILE`, `EVALRY_DEV_TENANTS=acme:alice` and optionally `EVALRY_OBJECT_DIR`; dev keeps state in memory.

| Variable | Meaning |
| --- | --- |
| `EVALRY_ENV` | `dev`, `staging`, `production`. Only production records official gate decisions |
| `EVALRY_OIDC_ISSUER`, `_AUDIENCE`, `_JWKS_URL` | Token verification (RS256/ES256). `EVALRY_TENANT_CLAIM` names the tenant claim (default `tenant_id`) |
| `EVALRY_DB_APP_DSN`, `EVALRY_DB_ADMIN_DSN` | Postgres as `evalry_app`; admin only for `bootstrap-tenant` |
| `EVALRY_GATE_KEY` | Signs gate decisions; required in production |
| `EVALRY_S3_BUCKET`, `_ACCESS_KEY`, `_SECRET_KEY`, `_ENDPOINT` | Object storage for run code; required in staging and production |
| `EVALRY_HOST`, `EVALRY_PORT` | Bind address (default `127.0.0.1:8000`). Put TLS and a body-size limit in front |

## 4. Use the API

Every call except `/healthz` needs `Authorization: Bearer <OIDC token>`; the tenant comes from the token, never the request.

| Call | Role | Purpose |
| --- | --- | --- |
| `POST /v1/projects` `{"project_id": "app"}` | tenant admin | Create a project |
| `PUT /v1/projects/{p}/members/{subject}` `{"role": "author"}` | admin | Grant a role. `DELETE` revokes |
| `PUT /v1/projects/{p}/suites/{version}` `{"tasks": [...]}` | author | Create an immutable suite version; returns its content hash |
| `GET /v1/projects/{p}/suites`, `.../suites/{version}` | viewer, author | List versions; read tasks with hidden tests |
| `POST /v1/projects/{p}/runs` (a run record) | author | Store a run produced elsewhere; run ids cannot be reused |
| `GET /v1/projects/{p}/runs`, `.../runs/{id}` | viewer | List and read runs |
| `POST /v1/projects/{p}/runs/{id}/gate` `{"rule": {...}, "exceptions": [...]}` | approver | Record a signed gate decision |
| `GET /v1/projects/{p}/decisions/{id}` | viewer | Read a decision |

Errors are JSON `{"error": "..."}` saying what happened and what to do: 401 sign in again, 403 role too low, 404 not found or not
yours, 409 already exists (runs, decisions and suites are immutable).

## 5. Develop

```
uv run ruff format . && uv run ruff check . && uv run mypy src tests && uv run pytest
```

Sandbox and Postgres tests need Docker and skip without it. Log only through `evalry.log.get_logger`. A score change fails
`tests/golden`; update `tests/golden/expected.json` only on purpose.
