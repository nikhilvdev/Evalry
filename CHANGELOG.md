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
- CLI `evalry run`, `evalry suite` and `evalry compare`.
- Golden runs in `tests/golden`: a score change fails the tests.

### Changed
- `python_tests` grader version is now "2": `rpc` isolation by default, and a test that cannot be proxied fails closed.
- `SampleResult` gains optional `image`, `seed`, `isolation`, `dataset_version` and `judge_version`. Records written before this still load
  as valid v1 records.

## 0.1.0.dev0 - unreleased

First development release. Task and run-record v1 schemas and LogQuill logging; no evaluation engine yet.
