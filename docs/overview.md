# What Evalry is

Evalry is a shared platform for evaluating AI-generated code. A team writes tasks (a prompt plus hidden tests), runs a model's
solutions against them in a sandbox, and gets scores, run-to-run comparisons and a signed pass/block decision they can put in CI.
Everything is scoped to a tenant and project, and every change and decision is recorded in a tamper-evident audit log.

Today it grades Python function tasks. Data evals, judges, repository and agent tasks are planned, not built.

## How a run flows

```
task YAML ──> suite (immutable version) ──> jobs (task x sample) ──> sandbox grading ──> run record ──> gate decision
                     │                            │                       │                  │              │
                registry (Postgres)         queue (Postgres)        two containers      Postgres +     signed, audited
                                                                    (see below)         object store
```

1. **Task**: YAML with an id, a prompt the model sees and `tests` it never sees.
2. **Suite**: a set of tasks frozen under a version label with a content hash. A version never changes.
3. **Job**: one task x sample. Its key is a hash of everything that can change the result, so retrying or resubmitting never
   duplicates work or cost.
4. **Grading**: the solution and the hidden tests run in two separate containers (no network, non-root, read-only filesystem,
   capped CPU, memory and processes, pinned image). A relay between them passes only JSON, so the solution cannot read its tests.
5. **Run record**: immutable JSON with every sample's score plus the task hash, grader version, image digest and seed that
   produced it. Comparing two runs uses a paired bootstrap over tasks.
6. **Gate**: pass or block against a rule that names an approved suite version. Production decisions are HMAC-signed.

## Components

| Part | What it does | Code |
| --- | --- | --- |
| CLI | Local runs, suites, compare, gate, migrate, bootstrap | `evalry.cli` |
| API | Multi-tenant HTTP under `/v1`, OIDC tokens, roles | `evalry.api` |
| Access layer | The one place roles and tenant scoping are enforced | `evalry.access` |
| Sandbox | Two-container grading | `evalry.runner` |
| Queue and workers | Idempotent jobs, priority, per-project concurrency and budget, leases | `evalry.scheduler`, `evalry.storage.pgjobs` |
| Storage | PostgreSQL (row-level security, immutable runs, decisions, suites, audit), content-addressed object storage | `evalry.storage` |
| Gates | Hard gate with exceptions that need a named approver and an expiry | `evalry.gates` |
| Audit | Hash-chained log, one chain per tenant | `evalry.audit` |

## Roles

| Role | Can |
| --- | --- |
| viewer | List and read runs and decisions, list suite versions |
| author | Everything a viewer can, plus create suites, record runs, read suite contents (hidden tests) |
| approver | Everything an author can, plus record gate decisions and approve gate exceptions |
| admin | Everything above, plus manage project members. Tenant admins also create projects |

A project you cannot see and one that does not exist return the same 404.

## Status

Built: local engine, sandbox, scoring and compare, hard gate, CI workflow, API with OIDC, Postgres storage with tenant isolation,
audit log, object storage for run code, durable queue, suite registry.
Not built yet: the worker process and enqueue endpoint (queued jobs cannot yet be executed end to end), live model providers,
judges and calibration, datasets, SSO login flow, Kubernetes runners, retention and deletion.
