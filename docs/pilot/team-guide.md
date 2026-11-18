# Pilot team guide

For the engineers on the pilot team. You write tasks, run evals and read results. You do not need to run the platform; the
operator does ([operator-runbook.md](operator-runbook.md)). To put the gate on a pull request, see [ci-gate.md](ci-gate.md).

Examples use `API=https://api.internal` and a token in `TOKEN`. Every call needs `Authorization: Bearer $TOKEN`.

## 1. How it fits together

1. You write **tasks**: a prompt plus **hidden tests**.
2. A set of tasks is frozen as an immutable **suite version** (`v1`, `v2`, ...).
3. A **run** gives every task's prompt to a generator, gets code back, and grades it in an isolated container with the hidden
   tests. The model never sees the tests.
4. A **gate rule** names the suite version and the pass rate you require. The **decision** is signed and audited: pass or block.

Nothing the generated code does can reach the network, your files or other tenants: it runs in a throwaway container with no
network and no secrets.

## 2. Writing a task

A task is one YAML file:

```yaml
id: parse_duration
language: python
prompt: |
  Write parse_duration(text) that converts strings like "1h30m", "45s" or "2d" to a number of seconds (int).
  Units are d, h, m, s and may be combined in that order. Raise ValueError for anything else, including the empty string.
tests: |
  assert parse_duration("45s") == 45
  assert parse_duration("1h30m") == 5400
  assert parse_duration("2d") == 172800
  assert parse_duration("1d1h1m1s") == 90061
  for bad in ["", "5", "h", "1x", "1m1h", "-5s"]:
      try:
          parse_duration(bad)
      except ValueError:
          pass
      else:
          raise AssertionError(bad)
tags: [parsing]
sensitivity: internal
owner: platform-team
```

| Field | Meaning |
| --- | --- |
| `id` | Unique within the suite |
| `language` | `python` in the pilot |
| `prompt` | The **only** thing the model sees. Give the exact function name and signature, the behaviour, and the edge cases that matter |
| `tests` | Hidden tests: plain Python, run with the function in scope. A failed `assert` or any exception fails the task |
| `tags`, `owner` | For your own coverage and routing |
| `sensitivity` | `public`, `internal`, `confidential` or `restricted`. Decides whether the prompt may go to the model provider (the operator sets the allowed levels; the default is `public` and `internal`) |
| `reference` | A known-good solution, kept with the task. It is not checked automatically yet; see below |

### What a task can and cannot do

The solution and the tests run in **separate containers** so the solution cannot read the tests. The price is that only simple
values cross between them:

- **Works:** calling functions with arguments and comparing results. Arguments and results may be numbers, strings, booleans,
  `None`, lists, dicts, tuples, sets, frozensets and bytes, nested.
- **Does not work (the task scores 0, it never silently passes):** tests that need a class or an instance, a callback, a
  generator, a module, or that check an argument was changed in place. Write the task so the function **returns** its result.
- No network, no files beyond a small scratch area, no clock or randomness you depend on. Tests must be deterministic.
- A grading run has a short time limit (10 seconds by default). Keep tests fast.
- The solution can see the *inputs* the tests call it with, never the expected values or whether a call passed. Do not put a
  secret in a test input.

### What makes a good task

- Comes from a real bug, review comment or spec, not from a puzzle.
- The prompt is unambiguous: a competent engineer could pass the tests from the prompt alone. If the tests check something the
  prompt does not say, the task is unfair.
- The tests catch the common wrong answers (off-by-one, wrong error type, empty input) but do not demand one particular
  implementation.
- It is neither too easy nor impossible. A task every attempt passes tells you nothing; one no attempt passes is probably wrong.

### Check each task before you add it

There is no automatic validation yet, so do these three checks by hand. Each takes seconds:

```
uv run evalry run parse_duration.yaml reference.py          # a correct solution: must exit 0
uv run evalry run parse_duration.yaml stub.py               # def parse_duration(text): pass   -> must exit 1
for i in 1 2 3; do uv run evalry run parse_duration.yaml reference.py; done    # same result every time: no flaky tests
```

Exit codes: `0` perfect score, `1` lower, `2` bad input (the message says what to fix). Needs Docker running locally.

Keep real secrets, customer data and production data **out of tasks and tests**. Use synthetic values.

## 3. Trying a set of tasks locally

Put one YAML per file in `tasks/`. Put recorded model replies in `replies.json`, one reply per sample, keyed by task id (fenced
code is fine; the last fenced block is used):

```
uv run evalry suite tasks/ --replies replies.json --samples 1 --suite-version v1 --out run.json --workers 4
```

It prints, for each task, the mean, min, max and standard deviation across samples, and pass@k. With several samples per task,
a task that sometimes passes and sometimes fails is telling you about the model, not the task.

## 4. Creating a suite on the platform

Convert the YAML files to one JSON body and create an immutable version (author role):

```
uv run python - <<'PY' > suite.json
import json, pathlib, yaml
tasks = [yaml.safe_load(p.read_text()) for p in sorted(pathlib.Path("tasks").glob("*.yaml"))]
print(json.dumps({"tasks": tasks}))
PY
curl -X PUT "$API/v1/projects/app/suites/v1" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' --data @suite.json
# {"suite_version":"v1","content_hash":"..."}
```

- A version can **never be changed**. To fix or add a task, create `v2`. Old runs and gate decisions stay tied to the version
  they used.
- Task ids must be unique, and a suite needs at least one task.
- Viewers can list versions but cannot read tasks: the hidden tests are visible to authors and above only.
- The `content_hash` identifies the exact content. A gate can be pinned to a version, never to "latest".

## 5. Running it

Start a run of a stored suite version:

```
curl -X POST "$API/v1/projects/app/run-requests" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"suite_version": "v1", "samples": 1, "priority": "smoke", "generator": "baseline-1"}'
# {"run_id":"...","jobs":12}
```

| Field | Meaning |
| --- | --- |
| `suite_version` | The version to run |
| `samples` | Attempts per task (1 to 100). Use 1 for a smoke gate; use more to see variance |
| `priority` | `smoke` runs ahead of `nightly` |
| `generator` | A label for the configuration being tested. It is part of the run's identity: the same suite and label is the same run |

Then poll until it finishes:

```
curl "$API/v1/projects/app/run-requests/$RUN_ID" -H "Authorization: Bearer $TOKEN"
# {"state":"running","jobs":{"queued":9,"running":3,"done":0}}  ...  {"state":"done","recorded":true}
```

States: `running`, `done` (the immutable run is now recorded), `failed` (a job failed all its retries; the response says which
task and why). Polling again is harmless. Read the run at `GET /v1/projects/app/runs/$RUN_ID`.

Things to know:

- **Re-running** the same suite and label after it is recorded returns `409`: results are immutable. Change the `generator`
  label to run again. Use a label that says what changed (`prompt-v7`, `model-b`).
- Starting the same run twice before it finishes does not duplicate work or cost.
- Each sample records its cost, the model name the provider returned, the image digest and the grading mode. Cost per run is in
  the run record and in the health report.
- There is no budget stop on this path yet. The operator's spend limit at the provider is your protection; start with small
  suites and `samples: 1`.

## 6. Gating

A gate rule says what must hold:

```json
{"name": "smoke", "suite_version": "v1", "grader_version": "2", "floor": 1.0, "min_pass_rate": 1.0, "required_tasks": ["parse_duration"]}
```

| Field | Meaning |
| --- | --- |
| `suite_version` | The suite the run must be for. A run of a different version blocks |
| `grader_version` | Must be `"2"` for the Python tests grader. Results from other grader versions are ignored |
| `floor` | A sample passes when its score is at least this (1.0 means all tests passed) |
| `min_pass_rate` | The share of samples that must pass |
| `required_tasks` | Tasks that must be present and pass |

An approver records the decision for a run:

```
curl -X POST "$API/v1/projects/app/runs/$RUN_ID/gate" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"rule": {...}}'
```

The response says `pass` or `block` with the reasons, who decided, when, and a signature. Read it later at
`GET /v1/projects/app/decisions/{id}`.

- Only samples graded with the default isolation count. Anything else blocks with a reason that mentions `rpc`.
- In the pilot's early weeks the platform is in **staging**: decisions are recorded as rehearsals. When the operator switches to
  **production**, decisions become official and signed with the gate key. You cannot ask for either; it is server
  configuration.
- A gate never gates on the judge, repository tasks or agent tasks in the pilot.

## 7. Reading the results

Each sample in a run has the code the model produced, the per-grader scores, the composite score, the cost and the versions that
produced it. Failures include a short tail of the test output as evidence.

For the bigger picture, anyone with viewer access can ask for the health report:

```
EVALRY_TOKEN=$TOKEN uv run evalry health --api $API --project app --html health.html
```

It shows flaky solutions (same code scoring differently across runs: investigate the task), tasks nearly every sample solves
(the suite is going stale), tasks no sample ever solved (check the task before trusting a failure), tag coverage, cost per run
and per solved task, and how many blocks stood after dispute.

## 8. A gate blocked your pull request

First decide what kind of failure it is. Open the run, find the failed tasks, and look at the code and the evidence.

| It looks like | It is | Do |
| --- | --- | --- |
| The generated code is wrong | A real failure | Fix the prompt, model or code. Run again with a new `generator` label |
| The code is reasonable, the test demands something the prompt never said | A task bug | Correct the task in a new suite version (dispute below) |
| It passed last time with the same code | A flaky task | Fix the nondeterminism, new suite version |
| The platform errored (`failed` state, provider errors) | Not a gate result | Re-run; tell the operator if it repeats |

### Disputing a failure

You can contest a **blocking** decision. The decision itself is never edited; the outcome is a separate record.

```
# author: contest it, naming the tasks you say were wrongly failed
curl -X POST "$API/v1/projects/app/decisions/$DECISION_ID/dispute" -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"reason": "<what the test requires that the prompt never said>", "task_ids": ["parse_duration"]}'
```

An approver who did not open the dispute resolves it at `POST .../decisions/{id}/dispute/resolution` with one of:

| Outcome | Meaning | Then |
| --- | --- | --- |
| `rejected` | The failure stands | Fix the code |
| `exception_approved` | The named approver waives the listed tasks until an expiry (at most 30 days) | Re-gate the same run with `"dispute_decision_id": "<decision id>"` in the gate request; that is a new signed decision showing the waiver |
| `task_corrected`, `judge_corrected`, `dataset_corrected` | The eval itself was wrong. Must name what changed, for example the new suite version | Run again against the corrected version |

One dispute per decision. A second contest means a new decision. Everything is audited, including who approved a waiver.

## 9. Data and privacy

- The prompt of each task is sent to the model provider through the generator service. Tests and expected values are never sent.
  Mark sensitive tasks with the right `sensitivity`; the service refuses levels the operator has not allowed.
- Solution code is stored (in object storage) with the run, scoped to your tenant and project.
- Logs never contain prompts, replies, code or tokens.
- Use synthetic data in tasks. Do not paste secrets, credentials or production data.

## 10. Quick answers

| Question | Answer |
| --- | --- |
| I changed a task, can I update `v1`? | No. Create `v2`; gates and old runs keep pointing at `v1` |
| My run returns `409` | That suite and `generator` label is already recorded. Use a new label |
| Why did a good-looking solution score 0? | Check for a class, callback or in-place mutation in the tests, a timeout, or code that does not define the function name from the prompt |
| Can the gate use the LLM judge? | Not in the pilot. A judge can only gate after it passes calibration against human labels |
| Can tier 3 or 4 tasks block a PR? | No. They are advisory in the pilot |
| Who can see my hidden tests? | Authors, approvers and admins of the project. Viewers cannot |
| How long does a smoke run take? | Mostly model latency plus one or two containers per sample. Measure it from your first runs |
