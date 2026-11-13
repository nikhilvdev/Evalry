# Changelog

## Unreleased

### Added
- Scoring: `evalry.scoring` (composite with hard floors, unbiased `pass_at_k`, per-task stats) and `evalry.compare` (seeded
  paired bootstrap, regression gate that refuses underpowered runs).
- Docker sandbox runner (`evalry.runner`) for untrusted code: pinned image digest, no network, non-root, read-only root, capped
  resources, fixed `PYTHONHASHSEED`. Default `rpc` isolation runs the solution and the hidden tests in separate containers joined
  by a size- and time-limited host relay with a strict tagged-JSON protocol; `inprocess` mode is kept, advisory only.
  Escape tests cover network, filesystem writes, environment, fork bomb and infinite loop.
- `python_tests` grader (`evalry.graders`).
- Generator plugin interface with a scripted `FakeGenerator`, `generate_logged` (LogQuill `llm_call`, never logs text) and
  `extract_code` (`evalry.generation`).
- Suite runs with N samples and per-task stats and `pass@k` (`evalry.suite`).
- Cost estimate and budget stop (`evalry.cost`); rates are inputs, there are no defaults.
- Job model and in-process queue (`evalry.jobs`, `evalry.worker`): idempotent keys, priority, per-project concurrency and
  budget limits, retries, crash recovery. `evalry suite` runs through it.
- Hash-chained audit log (`evalry.audit`), mirrored to LogQuill with `TamperEvidentPlugin`; `--audit-log`, `--project`, `--actor`.
- Grading cache (`evalry.cache`, `--cache-dir`); `evalry.engine` holds `grade` and `run_sample`.
- Hard gate (`evalry.gates`, `evalry gate`): approved suite version, `rpc`-isolation results only, exceptions with a named approver
  and expiry, HMAC-signed immutable decisions; production records `gate.decided`, other environments `gate.rehearsed`.
- Parallel workers (`evalry suite --workers N`, `work(..., workers=N)`): threads share the queue, per-project concurrency limits
  still apply, and log records in worker threads keep `run_id` and `suite_version`.
- Roles and tenant scoping (`evalry.access`): viewer/author/approver/admin per project, tenant admins, immutable runs and gate
  decisions, unknown and foreign projects indistinguishable, audited denials. Tests prove cross-tenant reads and writes fail.
- Service layer (`evalry.service`): `submit_suite` and `gate_run` check roles before any work starts; gate exceptions are rejected
  (and audited) unless the named approver holds the approver role. Job keys, limits and budgets are keyed by tenant as well as project.
- HTTP API skeleton (`evalry.api`): FastAPI under `/v1`, OIDC bearer-token verification with an asymmetric-only algorithm
  allow-list, tenant taken from a verified claim, role and tenant isolation over HTTP, signed gate decisions with environment
  and key from server config, request ids, request logs without tokens or raw ids. New dependencies: fastapi, pyjwt[crypto], uvicorn (dev: httpx).
- PostgreSQL storage: schema `0001_init.sql` (tenant in every primary key, composite foreign keys, row-level security forced on
  every tenant table, update/delete triggers making runs and gate decisions immutable, least-privilege grants), `PostgresBackend`
  (refuses superuser connections), checksum-verified plain-SQL migrations, `evalry migrate`, `deploy/docker-compose.yml`.
  The access layer now sits on a `Backend` interface and a decision must reference a stored run. New dependency: psycopg[binary,pool].
- Object storage (`evalry.objects`): content-addressed (SHA-256) blobs under tenant/project prefixes, ids validated against path
  escape, reads verified against their digest. `FileObjectStore` for dev, `S3ObjectStore` for S3-compatible services; MinIO added
  to `deploy/docker-compose.yml`. New dependency: boto3.
- Durable job queue (`0003_jobs.sql`, `evalry.storage.pgjobs.PostgresJobQueue`): same methods as the in-process queue
  (`JobStore` protocol, which the worker now accepts), idempotent submit, priority then FIFO, per-project concurrency and budget
  limits enforced across processes by a claim lock, leases so a dead worker's job is requeued, and a trigger making a done job
  final. The API role is tenant-bound by row-level security and can only enqueue and read; claiming across tenants needs the new
  `evalry_worker` role (jobs table only; created in `deploy/postgres-init`, new `EVALRY_PG_WORKER_PASSWORD`). Not yet reachable
  from the CLI or the API.
- Suite registry (`0004_suites.sql`, `PUT/GET /v1/projects/{p}/suites/{version}`): a suite version is an immutable task list with
  a content hash, created by authors and audited. Hidden tests are readable by author or higher only (viewers list versions).
  Workers read suites across tenants but cannot write them. This is what a worker needs to grade queued jobs.
- Runs use object storage: with an object store configured, `put_run` writes each sample's code to it and keeps only
  `SampleResult.code_digest` (new optional field) in the run row; `get_run` puts the code back after the READ role check and
  fails loudly if an object is missing. Server config: `EVALRY_S3_BUCKET`, `EVALRY_S3_ACCESS_KEY`, `EVALRY_S3_SECRET_KEY`,
  optional `EVALRY_S3_ENDPOINT` (required in staging and production); dev may use `EVALRY_OBJECT_DIR`.
- Audit log in PostgreSQL (`0002_audit.sql`, `PostgresAuditSink`): one hash chain per tenant, immutable by trigger, row-level
  security, per-chain advisory lock so several API processes never fork a chain. `AuditLog` now takes a file or a database sink.
- Server entry point (`evalry-api`, `python -m evalry.api`): settings from environment variables, all problems reported at once
  without echoing secrets, PostgreSQL required outside dev, loopback by default; `evalry bootstrap-tenant`; database outages
  return 503.
- CLI `evalry run`, `evalry suite` and `evalry compare`.
- Golden runs in `tests/golden`: a score change fails the tests.

### Changed
- The sandbox waits until killed containers are actually removed before returning, so "cleaned up" is true when the call ends.
- `python_tests` grader version is now "2": `rpc` isolation by default, and a test that cannot be proxied fails closed.
- `SampleResult` gains optional `image`, `seed`, `isolation`, `dataset_version` and `judge_version`. Records written before this still load
  as valid v1 records.

## 0.1.0.dev0 - unreleased

First development release. Task and run-record v1 schemas and LogQuill logging; no evaluation engine yet.
