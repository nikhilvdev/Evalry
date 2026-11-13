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
- API to worker path (`0005_job_run.sql`): `POST /v1/projects/{p}/run-requests` queues a stored suite version (author or higher;
  the run id derives from tenant, project, suite version and generator name, so resubmitting duplicates nothing and a recorded
  run answers 409). `GET .../run-requests/{run_id}` reports progress and, once every job is done, records the immutable run,
  because the worker role cannot write runs. Jobs now carry `run_id` and `suite_version`. `evalry worker` (env
  `EVALRY_DB_WORKER_DSN`, `--once` to exit when idle) claims jobs as `evalry_worker`, reads the suite from the database and grades
  in the sandbox. The generator is still the scripted fake (`--replies`); the generator name in the request is not checked
  against the worker's configuration. The API gets the PostgreSQL queue whenever `EVALRY_DB_APP_DSN` is set.
- Generator service (`evalry-generator`, `evalry.generation.service`): the only process holding `ANTHROPIC_API_KEY`. Workers call
  `POST /v1/generate` with a shared bearer token through `RemoteGenerator` (https, or http to localhost only), so no worker or
  runner host sees a provider key. `AnthropicGenerator` records the model the API returned, tokens and cost from configured rates;
  there is no default model or price (`EVALRY_GEN_MODEL`, `EVALRY_GEN_USD_PER_MTOK_IN/OUT`, `EVALRY_GEN_TOKEN`). No fallback model
  and no sampling parameters: a safety refusal fails the job. Tasks above `EVALRY_GEN_ALLOWED_SENSITIVITY` (default public,internal)
  are refused before anything is sent. `evalry worker --generator-url` uses it; `--replies` keeps the offline fake. New dependency: anthropic.
- Judge service (`evalry-judge`, `evalry.judge`): same shape as the generator service. A judge is a versioned asset (`judge_version`
  hashes model, system prompt and the rubric file the eval owner supplies); solution code is delimited untrusted data, the
  verdict is a schema-checked 1 to 5 score, and a malformed verdict fails the job rather than scoring. `run_sample` judges only
  solutions that pass every deterministic grader and records the raw judge score, but its composite weight defaults to 0
  (`evalry worker --judge-url`, `--judge-weight`), so an uncalibrated judge stays advisory. `evalry.judge.calibration.calibrate`
  measures the plan's targets (agreement within one point >= 0.75, stability, injection canary) against 20+ human labels; a
  `HardGateRule` naming the judge is rejected until calibration is recorded and enforced (phase 3). Compose gains a `judge` service.
- Judge calibration workflow (`0006_calibrations.sql`): `evalry judge-calibrate LABELS.json --judge-url ...` runs the plan's targets
  (agreement within one point >= 0.75, stability >= 0.9, injection canary) over 20+ human labels and writes a report tied to the
  judge version and the labeled set's hash. An approver uploads it to `POST /v1/projects/{p}/judge-calibrations`; the server
  recomputes `passed` from the numbers, stores an immutable audited record (`GET` lists them), and the newest record per judge
  version decides. `gate_run` (and `decide`) refuse a rule on the judge unless its version has a passing record younger than 90 days,
  so a failing recalibration or a stale pass revokes gating. Replaces the earlier blanket refusal of judge rules.
- Human labeling workflow (`0007_labeling.sql`, `evalry.labeling`): an approver creates a blind batch (`POST /v1/projects/{p}/label-batches`:
  items are prompt and code only, the schema rejects generator or judge-score fields) under a written guideline; a deterministic
  share (`double_share`, default 0.2) needs two reviewers. Authors pull their next unscored item (`GET .../next`) and score it
  once (`PUT .../items/{i}/label`); labels are append-only and audited without the score. When two reviewers differ, an approver
  who is not one of them adjudicates (`PUT .../adjudication`); scores are never averaged. `GET .../label-batches/{b}` reports
  agreement between reviewers and flags a guideline that needs work (within-one agreement < 0.75), and `.../export` returns the
  golden labels in the exact shape `evalry judge-calibrate` reads, refusing (409) until every item is final.
- Dispute workflow (`0008_disputes.sql`, `evalry.disputes`): an author contests a blocking decision
  (`POST /v1/projects/{p}/decisions/{d}/dispute`, naming the tasks that ran and were failed); one dispute per decision, and the
  signed decision is never touched. An approver who did not open it resolves it (`.../dispute/resolution`): `rejected`; an
  `exception_approved` outcome that yields real `GateException`s (the resolver is the named approver, tasks must be the contested
  ones, expiry within 30 days); or `task_corrected` / `judge_corrected` / `dataset_corrected`, which must name what changed
  (a task correction must point at an existing suite). Applying an exception is a new gate decision for the same run
  (`dispute_decision_id` on the gate request), so it is signed and audited like any other. Everything is append-only.
- Eval health (`evalry.health`, `GET /v1/projects/{p}/eval-health`, `evalry health`): measured from stored records, readable by
  any viewer. Flakiness (the same code scoring differently across runs), suite saturation and never-solved tasks, coverage
  by language, tier and tag with thin areas and regression-gate readiness (100 tasks), judge calibration health (failing, stale,
  expired, agreement drift, uncalibrated judges in use), how many blocking decisions stood after dispute, and cost per run and
  per solved task, with a severity-sorted list of open issues. `evalry health --api URL --project P` (token in
  `EVALRY_TOKEN`) prints a summary or writes a self-contained HTML dashboard (`--html`, no scripts or network, light and
  dark), and `--fail-on critical|warning` makes it usable in CI. Not measured yet: time to result, drift on a stability set,
  and production-versus-offline comparison.
- Production monitoring loop (`0009_production.sql`, `evalry.monitoring`): production systems push masked outcome signals
  (`POST /v1/projects/{p}/production-signals`: accepted, edited, rejected, build failed or reverted, keyed by suite task id; the
  schema has no field for code or user content, ids make resending harmless) and `GET .../production-monitor?days=N` compares
  each task's offline score in the latest run with its production outcomes (20+ signals needed), reporting tasks that pass
  offline but fail in production, the reverse, and the offline-to-production rank correlation. A finding becomes an eval incident
  an approver opens (`POST .../eval-incidents`, one open per task) and closes either by adding tasks that reproduce the failure
  to an existing suite version or by saying why it is not an eval problem (`.../closure`). Findings and open incidents feed
  eval health as issues, and `evalry monitor --fail-on-finding` is the cron or CI alert. Built ahead of the phase gate at the
  user's explicit request; nothing samples real code, and per-project retention of signals is not built.
- Repository and agent tasks (tiers 3 and 4, plan §4), built ahead of the phase gate at the user's explicit request. A `Task` may
  carry `repo` (snapshot digest, commit, digest-pinned image with the dependencies, test command) and, for tier 4, `agent` limits;
  `tests` then holds the hidden test files as a JSON object. The fields are omitted from tier 1 and 2 JSON, so their hashes and
  stored records do not change. `evalry snapshot DIR --tenant T --project P` packs a deterministic tar.gz (no `.git`, no
  symlinks), validates it and stores it by SHA-256. Tier 3: the model sees the issue and the repository's text files and
  answers with `<file path="...">` blocks; edits are validated (safe relative paths, size limits), laid over the snapshot, the
  hidden tests are laid over those so they cannot be replaced, and the repo's own test command runs in one hardened container
  (no network, non-root, read-only root, 1 GB, 256 processes, no volumes) that is handed a single validated tar stream. Tier 4:
  an agent with a fixed tool set (`bash`, `read_file`, `write_file`; defined by the service, never the caller) works in a
  long-lived hardened container through the generator service's new `/v1/agent-step`, under step, wall-time and cost limits;
  its diff is graded in a fresh container with the hidden tests, which never enter the agent's workspace. Agent metrics (steps,
  tool calls, tokens, wall time) and failure categories (step, time and cost limits, command timeout, looped, unsafe action
  attempted, never ran tests, model refused, tests failed) are recorded on the sample result. Repository results are tagged
  `inprocess` isolation because edited code and hidden tests share a container, so they can never hard-gate. Not built: repo
  tasks through the local `evalry suite` command, a grading cache for them, non-Python language profiles beyond what the image
  provides, and deletion edits in the tier 3 reply format.
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
