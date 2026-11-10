# Changelog

## Unreleased

### Added
- Scoring: `evalry.scoring` (composite with hard floors, unbiased `pass_at_k`, per-task stats) and `evalry.compare` (seeded
  paired bootstrap, regression gate that refuses underpowered runs).
- Docker sandbox runner (`evalry.runner`) for untrusted code: pinned image digest, no network, non-root, read-only root, capped
  resources, fixed `PYTHONHASHSEED`. Hidden tests are never written to disk and arrive only after the solution has imported.
  Escape tests cover network, filesystem writes, environment, fork bomb and infinite loop.
- `python_tests` grader (`evalry.graders`).
- Generator plugin interface with a scripted `FakeGenerator`, `generate_logged` (LogQuill `llm_call`, never logs text) and
  `extract_code` (`evalry.generation`).
- Suite runs with N samples and per-task stats and `pass@k` (`evalry.suite`).
- Cost estimate and budget stop (`evalry.cost`); rates are inputs, there are no defaults.
- CLI `evalry run`, `evalry suite` and `evalry compare`.
- Golden runs in `tests/golden`: a score change fails the tests.

### Changed
- `SampleResult` gains optional `image`, `seed`, `dataset_version` and `judge_version`. Records written before this still load
  as valid v1 records.

## 0.1.0.dev0 - unreleased

First development release. Task and run-record v1 schemas and LogQuill logging; no evaluation engine yet.
