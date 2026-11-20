# Operator runbook for the pilot

For the person who stands up and runs the platform. The pilot team does not need this page.

Commands are run from the repository root with `uv run` (Python 3.12 and [uv](https://docs.astral.sh/uv/)). Nothing here
commits, publishes or sends data anywhere except the model provider you configure.

## 1. What you are running

| Piece | What it is | Holds |
| --- | --- | --- |
| API (`evalry-api`) | FastAPI service, `/v1/...` | Verifies tokens, enforces roles and tenants, records decisions. The only component the team talks to |
| PostgreSQL | System of record | Projects, roles, suites, runs, decisions, jobs, the audit log |
| Object storage (MinIO or S3) | Content-addressed blobs | Solution code from runs; repository snapshots |
| Worker (`evalry worker`) | Claims jobs, runs the sandbox | The `evalry_worker` database role. Needs Docker |
| Generator service (`evalry-generator`) | The only holder of the model provider key | `ANTHROPIC_API_KEY` |
| Judge service (`evalry-judge`) | Same, for the judge | Not used for gating in the pilot |
| Sandbox containers | One per grading process | Nothing: no network, no secrets, no volumes |

Keys never reach a worker or a sandbox: the worker calls the generator service with a shared token.

## 2. Before you start

Work through the blocking items in [README.md](README.md). In particular you need:

- **A dedicated runner node** for the worker and its Docker daemon. Access to the Docker socket is root-equivalent, and the
  worker holds a database role that can read every tenant's suites (with hidden tests). Do not run it on the API or database
  machine or on a developer workstation.
- **An OIDC provider** and its issuer, audience and JWKS URL. Tokens must be RS256 or ES256 and carry a tenant claim
  (`tenant_id` by default; set `EVALRY_TENANT_CLAIM` to rename it).
- **The model, its prices per million tokens, and a spend limit set at the provider.** There are no defaults in the code.
- **TLS in front of the API**, and a request-body limit at the proxy. The API binds to loopback unless you change it.

## 3. Stand up the data layer

```
cp deploy/.env.example deploy/.env      # set every password and key; never commit it
```

Set the Postgres passwords (`EVALRY_PG_ADMIN_PASSWORD`, `EVALRY_PG_APP_PASSWORD`, `EVALRY_PG_WORKER_PASSWORD`) and the object
storage keys. Then start Postgres and MinIO only:

```
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --wait postgres minio
```

Compose refuses to run if **any** required variable in the file is unset, even for a service you are not starting. The
example file defines them all, so keep the placeholder values for the generator and judge until you configure them
(section 5). Naming the services chooses what starts; it does not skip the check. The compose file is a single-host setup for
the pilot, not a production topology: no replication, no backups, no TLS.

Create the object-storage bucket (the API refuses to start in staging or production without one) and apply the migrations:

```
export EVALRY_DB_ADMIN_DSN='host=127.0.0.1 user=postgres password=... dbname=evalry'
EVALRY_ADMIN_DSN="$EVALRY_DB_ADMIN_DSN" uv run evalry migrate     # exit 0 = up to date
```

Migrations are checksummed. If one fails, read the message: it says what to do. Never edit an applied migration file.

## 4. Start the API

```
export EVALRY_ENV=staging                       # see "Staging versus production" below
export EVALRY_DB_APP_DSN='host=127.0.0.1 user=evalry_app password=... dbname=evalry'
export EVALRY_DB_ADMIN_DSN='...'                # only needed for bootstrap-tenant
export EVALRY_OIDC_ISSUER=https://login.example.com
export EVALRY_OIDC_AUDIENCE=evalry-api
export EVALRY_OIDC_JWKS_URL=https://login.example.com/.well-known/jwks.json
export EVALRY_S3_BUCKET=evalry EVALRY_S3_ENDPOINT=http://127.0.0.1:9000 \
       EVALRY_S3_ACCESS_KEY=... EVALRY_S3_SECRET_KEY=...
uv run evalry-api
```

Configuration problems are all reported at once, and secrets are never printed. Check it is up:

```
curl -s http://127.0.0.1:8000/healthz        # {"status":"ok"}
```

Put it behind your TLS proxy and set a body-size limit there (the in-app check only reads `Content-Length`).

### Staging versus production

`EVALRY_ENV` is server configuration, never a request field. Gate decisions are recorded as **rehearsals** in `dev` and
`staging`, and as **official, signed decisions** only in `production`, which also requires `EVALRY_GATE_KEY`:

```
export EVALRY_GATE_KEY=$(openssl rand -hex 32)   # store it in your secret manager; losing it means old signatures cannot be re-verified
```

Run the pilot's first weeks on `staging`. When the team is ready for an official gate (week 6 in the suggested timeline),
restart the API with `EVALRY_ENV=production` and the key. Rehearsal decisions already recorded stay recorded as rehearsals.
If you want a hard separation, run a second API and database for production instead of switching.

## 5. Services that hold keys

### Generator service

```
# in deploy/.env: ANTHROPIC_API_KEY, EVALRY_GEN_MODEL, EVALRY_GEN_USD_PER_MTOK_IN/OUT, EVALRY_GEN_TOKEN (32+ chars)
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build generator
curl -s http://127.0.0.1:8001/healthz
```

- It refuses to start without a model, both prices and a token. Take prices from the provider's current price page.
- `EVALRY_GEN_ALLOWED_SENSITIVITY` (default `public,internal`) decides which task sensitivity levels may be sent out. A task
  tagged `confidential` or `restricted` is refused before anything leaves.
- A safety refusal from the model fails the job; there is no fallback model by design.
- It is published on `127.0.0.1:8001` only. The worker reaches it over loopback or a private link with `https` (plain `http` is
  accepted for localhost only).
- Prompt and reply text are never logged.

Make one real call with a **synthetic task** before the team starts, to confirm cost and the returned model name match what
the provider's dashboard shows. Do not use proprietary code for this check.

The judge service is built but the pilot gate does not use it, so you can leave it stopped.

## 6. Create the tenant, the project and the roles

```
uv run evalry bootstrap-tenant acme alice      # alice = the identity provider subject of the first admin
```

This needs the admin DSN, is an operator action, and is not reachable over HTTP. From here the **tenant admin** (alice) uses
the API with their own token:

```
curl -X POST  $API/v1/projects -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"project_id": "app"}'
curl -X PUT   $API/v1/projects/app/members/bob   -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"role": "author"}'
curl -X PUT   $API/v1/projects/app/members/carol -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"role": "approver"}'
```

Subjects are the token's `sub` claim. Project ids are lowercase letters, digits and hyphens. Roles: `viewer`, `author`,
`approver`, `admin`. Tenant admins hold every role on every project in the tenant.

**The CI identity.** The CI job needs an identity that can start runs and record decisions. Create a dedicated service
identity at your provider, give it the `approver` role on this one project only, and keep its token in the CI secret store.
It can then record gate decisions for that project; treat it like a deploy key. See [ci-gate.md](ci-gate.md).

## 7. Start the worker on the runner node

On the runner node (Docker running, repository checked out, `uv sync` done):

```
export EVALRY_DB_WORKER_DSN='host=... user=evalry_worker password=... dbname=evalry'
export EVALRY_GEN_TOKEN=...                      # same value as the generator service
uv run evalry worker --generator-url https://generator.internal:8001 --workers 4
```

- `--workers N` is how many jobs run at once in this process. Each grading uses one or two containers (about 256 MB each).
- Without `--generator-url`, `--replies file.json` runs scripted, recorded replies. That is how you rehearse without spending.
- It polls the queue forever. `--once` runs until the queue is empty and exits, for tests.
- Run it under a supervisor (systemd, for example) so it restarts. A job whose worker died is requeued when its lease expires.
- The worker only grades jobs; it never records runs. The API does that when the team polls a run's status.

### Node settings

| Variable | Meaning |
| --- | --- |
| `EVALRY_MAX_CONTAINERS` | Containers allowed at once per worker process (default 16). Run one worker process per node, or split the capacity |
| `EVALRY_RUNNER_IMAGES` | Extra images allowed for repository tasks, exact digest-pinned references or registry prefixes ending in `/`. Whoever adds one owns its contents |
| `EVALRY_DOCKER_RUNTIME` | For example `runsc` to use gVisor, if installed |
| `EVALRY_SECCOMP_PROFILE`, `EVALRY_APPARMOR_PROFILE` | Seccomp defaults to the strict profile shipped with Evalry; set a file path to use your own or `default` for Docker's. AppArmor is off unless a loaded profile is named. A setting that cannot be honoured is an error, never a silent fallback |
| `EVALRY_OBJECT_DIR` or `EVALRY_S3_*` | Only needed by workers running repository tasks, to read snapshots |

Run `uv run evalry node-check` on the node before the first job and keep the output: it starts a probe container with the real
job flags and reports what the sandbox actually enforces (non-root, no capabilities, no network, read-only root, memory and
process limits, seccomp) and how isolated the daemon is. Any FAIL must be fixed. The shared-kernel WARN is a decision to record
with your security owner; `--strict` turns it into a failure for nodes that must be rootless or use gVisor. Add
`--require-node-check` to `evalry worker` to refuse to start on a failing node.

A strict seccomp profile is on by default (it refuses `ptrace`, `process_vm_readv/writev` and non-IP sockets);
`EVALRY_SECCOMP_PROFILE=default` selects Docker's own profile if an approved image needs more.

Add `evalry reap` to cron (every few minutes) on the runner node. It removes sandbox containers whose own deadline has passed,
which can happen after a worker crash. It never touches a job still inside its deadline.

## 8. Check that everything works end to end

1. `curl /healthz` on the API and the generator.
2. As an author, create a tiny suite, request a run with the `--replies` worker (no spend), poll it to `done`, and gate it. The
   exact calls are in the team guide, sections 4 and 5.
3. Repeat with `--generator-url` and a synthetic task, to prove the real path.
4. Confirm a decision came back signed and appears in `GET /v1/projects/app/decisions/{id}`.
5. Run `uv run pytest tests/sandbox -q` on the runner node and keep the output for the security review.

## 9. Day to day

| Task | How |
| --- | --- |
| See how the platform and the suite are doing | `EVALRY_TOKEN=... uv run evalry health --api https://api.internal --project app --html health.html` |
| Fail a CI job when health is critical | add `--fail-on critical` |
| Watch for a worker that is stuck | a run that stays `running` with all jobs `queued`: check the worker is up and can reach Docker and the database |
| Orphaned containers | `docker ps -a --filter name=evalry-`, then `evalry reap` |
| Rotate the generator token | change it in `deploy/.env` and on the worker, restart both |
| Add a person | `PUT /v1/projects/{p}/members/{subject}` as a project or tenant admin |
| Remove a person | `DELETE` the same path. Revoking is audited |
| Upgrade | pull, `uv sync`, `evalry migrate`, restart the API, then the workers. Migrations are forward-only |

Logs are JSON lines on standard output with the tenant, project, run and job ids attached; prompts, replies, tokens and code
are never in them. The audit log is append-only in Postgres.

### Backups (stopgap)

There is no backup procedure in the product yet. Until there is, take a regular dump and test a restore yourself:

```
pg_dump "host=... user=postgres dbname=evalry" -Fc -f evalry-$(date +%F).dump
```

Back up the object storage bucket too. Treat the pilot as recoverable by re-running, not as a system of record.

## 10. Troubleshooting

| You see | Cause and what to do |
| --- | --- |
| API exits at start with a list of problems | Fix every listed variable. Secrets are never echoed |
| `401` with "access token is missing, invalid or expired" | The token is not from the configured issuer and audience, is expired, or lacks the tenant claim. Check `EVALRY_OIDC_*` and `EVALRY_TENANT_CLAIM` |
| `403` "Your role on project ... is viewer" | The role is too low. A project admin grants a higher one |
| `404` for a project that exists | It belongs to another tenant, or the id is wrong. The platform never says which |
| `503` "The database is unavailable" | Postgres is down or the app DSN is wrong. The API retries on the next request |
| A run stays `running`, jobs stay `queued` | No worker is running, or it cannot reach Postgres or Docker. Start it and check its output |
| Worker: "Runner is at its container limit" | `EVALRY_MAX_CONTAINERS` reached. The job is retried. Raise it if the node has room |
| "Image ... is not on the runner image allowlist" | A repository task names an image nobody approved. Review it, then add it to `EVALRY_RUNNER_IMAGES` |
| Job failed with a generator error `401`, `403`, `429` or `502` | `401`: the worker's `EVALRY_GEN_TOKEN` does not match. `403`: the task's sensitivity is not allowed. `429` and `502`: the provider is limiting or failing; the job retries |
| Gate blocks with "rpc" in the reason | The run was not graded with `rpc` isolation, so it cannot gate. Re-run with the default isolation |
| `409` on a run request | The run is already recorded and immutable. Use a different generator name to run again |
| Tests that print or loop forever | They are cut to a short tail and killed at the timeout; the container is removed |
