# Putting the gate on a pull request

For the pilot team's CI owner. Read [team-guide.md](team-guide.md) first: you need a suite and a gate rule.

## 1. Choose what you are gating

There are two ways to feed the gate, and they answer different questions.

| | Shape A: grade **your system's** output | Shape B: grade **the platform's** model |
| --- | --- | --- |
| Question | "Does the code our AI feature or agent produces pass our hidden tests?" | "Does the model and prompt the platform is configured with pass them?" |
| Who generates the code | Your pipeline writes one reply per task prompt into `replies.json` | The platform's generator service, called by a worker |
| Who grades | The CI runner, in a sandbox container, with the Evalry CLI | The platform's worker, on the runner node |
| Who holds model keys | Whatever your pipeline already uses | Only the generator service |
| Where the decision is made | In CI (`evalry gate`), signed with a key held as a CI secret | On the platform, signed with the server's key |
| Needs on the CI runner | Docker, Python 3.12, `uv`, a checkout of Evalry | `curl`, `jq`, a token |

**For the pilot's goal of blocking a pull request that carries failing AI-generated code, use Shape A.** Shape B is for
comparing models or prompts the platform controls, and for gates whose result you want vouched for by the platform rather than
by the pipeline.

## 2. Who vouches for the decision

Be clear about this before you make the gate required.

- **Shape A decisions are as trustworthy as your workflow.** The grading and the gate run inside your CI job, and the signing
  key is a CI secret. Anyone who can change the workflow, `replies.json` or the gate rule on the protected branch can make the
  gate pass. Protect it: require review for changes to the workflow, the rule and the suite directory (CODEOWNERS plus branch
  protection), and only give the signing key to jobs that run on the protected branch.
- **Pull requests are rehearsals.** Run `--environment staging` on pull requests: it needs no key, so it works for forks, and
  it records a rehearsal. Record the official, signed decision (`production`) only on pushes to the protected branch.
- **Importing a CI run into the platform** (`POST /v1/projects/{p}/runs`) stores it, and the platform will gate it, but the
  platform cannot verify that the run really came from a sandbox. Treat that as record keeping, not as independent proof.
- **Shape B results come from the platform's own workers**, so the CI job holds only an API token.

## 3. Shape A: a workflow in your repository

Evalry is not installed on a CI runner by default and is not published as a package. The workflow checks it out next to your
code. (The `eval-gate.yml` workflow inside the Evalry repository checks out the calling repository and runs `uv sync` there, so
it works inside the Evalry repository itself, not when called from another one. Copy the steps below instead.)

```yaml
name: AI code gate
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  gate:
    runs-on: ubuntu-latest            # Docker is available on GitHub-hosted runners
    timeout-minutes: 15
    env:
      SUITE_VERSION: v1               # must match the version in your gate rule
      PROJECT: app
      ENVIRONMENT: ${{ github.event_name == 'pull_request' && 'staging' || 'production' }}
      EVALRY_GATE_KEY: ${{ github.event_name == 'push' && secrets.EVALRY_GATE_KEY || '' }}   # pull requests never see the key
    steps:
      - uses: actions/checkout@v7
      - uses: actions/checkout@v7
        with:
          repository: your-org/evalry          # your internal copy of Evalry
          path: .evalry
          token: ${{ secrets.EVALRY_REPO_TOKEN }}
      - uses: actions/setup-python@v7
        with:
          python-version: "3.12"
      - run: pip install uv && uv sync --project .evalry --locked

      - name: Generate a reply for every task prompt (your step)
        run: ./ci/generate_replies.sh tasks/ replies.json

      - name: Grade in the sandbox
        run: >
          uv run --project .evalry evalry suite tasks/ --replies replies.json --suite-version "$SUITE_VERSION"
          --project "$PROJECT" --actor "github:${{ github.actor }}" --audit-log audit.jsonl --out run.json

      - name: Decide the gate
        run: >
          uv run --project .evalry evalry gate run.json --rule gate.yaml --environment "$ENVIRONMENT"
          --project "$PROJECT" --actor "github:${{ github.actor }}" --audit-log audit.jsonl --out decision.json

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: gate-evidence
          path: |
            run.json
            decision.json
            audit.jsonl
```

Your part is `ci/generate_replies.sh`: for each task in `tasks/`, take its `prompt`, run it through the system you are testing,
and write `replies.json` as `{"<task id>": ["<reply for sample 0>", ...]}`. The reply may contain fenced code; the last fenced
block is used. **Give your system the prompt only, never the task's `tests`.**

How the steps behave:

- `evalry suite` exits 0 when it ran, whatever the scores; `evalry gate` is what fails the job: exit `0` pass, `1` blocked,
  `2` bad input.
- A pull request records a staging rehearsal. A push to `main` records the official decision and needs `EVALRY_GATE_KEY`
  (a random 32-byte value, kept only as a secret on the protected branch).
- The artifacts are the evidence: the run record, the signed decision and the audit log. Keep them.
- Add `--samples N` to `suite` to see variance, and read the gate rule's `min_pass_rate` accordingly.

To also keep the result on the platform, after the gate step post the run to it (author role) and record a decision (approver
role) with your CI identity; see the caveat in section 2.

```
curl --fail-with-body -X POST "$EVALRY_API/v1/projects/$PROJECT/runs" -H "Authorization: Bearer $EVALRY_TOKEN" \
  -H 'content-type: application/json' --data @run.json
```

Run ids must be unique, so a re-run of the same job needs a new id; the CLI generates a fresh one each time.

## 4. Shape B: let the platform generate and grade

Template script. It needs `curl` 7.76 or newer and `jq`. It was run end to end against a local API with a worker and the real
sandbox (a passing run exits 0, a failing one exits 1 with the reasons, and re-using a recorded label exits 2 with the API's
message), but not against your identity provider or your deployment, so rehearse it on staging first.

```bash
#!/usr/bin/env bash
# Request a run, wait for it, gate it. Exit 0 = pass, 1 = blocked, 2 = could not get a result.
set -euo pipefail
: "${EVALRY_API:?}" "${EVALRY_TOKEN:?}" "${PROJECT:?}" "${SUITE_VERSION:?}" "${GATE_RULE:?}"   # GATE_RULE: a JSON file
GENERATOR="${GENERATOR:-ci-${GITHUB_SHA:-local}}"     # part of the run's identity: a new label for every configuration
SAMPLES="${SAMPLES:-1}"
TIMEOUT_S="${TIMEOUT_S:-600}"
api() {   # prints the response; on an HTTP error prints the API's message (it says what to do) and fails
  local out
  out=$(curl --fail-with-body -sS -H "Authorization: Bearer ${EVALRY_TOKEN}" -H 'content-type: application/json' "$@") || { echo "${out:-request failed}" >&2; return 1; }
  printf '%s' "$out"
}

body=$(jq -n --arg v "$SUITE_VERSION" --arg g "$GENERATOR" --argjson s "$SAMPLES" \
  '{suite_version: $v, samples: $s, priority: "smoke", generator: $g}')
run_id=$(api -X POST "$EVALRY_API/v1/projects/$PROJECT/run-requests" -d "$body" | jq -r .run_id) || exit 2

deadline=$((SECONDS + TIMEOUT_S))
while :; do
  status=$(api "$EVALRY_API/v1/projects/$PROJECT/run-requests/$run_id") || exit 2
  state=$(jq -r .state <<<"$status")
  [ "$state" = done ] && break
  [ "$state" = failed ] && { echo "run failed: $(jq -r .error <<<"$status")" >&2; exit 2; }
  [ "$SECONDS" -ge "$deadline" ] && { echo "timed out waiting for run $run_id" >&2; exit 2; }
  sleep 10
done

decision=$(api -X POST "$EVALRY_API/v1/projects/$PROJECT/runs/$run_id/gate" \
  -d "$(jq -n --slurpfile r "$GATE_RULE" '{rule: $r[0]}')") || exit 2
jq -r '"gate \(.gate): \(.outcome | ascii_upcase) (\(.environment))", (.reasons[] | "  - " + .)' <<<"$decision"
[ "$(jq -r .outcome <<<"$decision")" = pass ]
```

Notes:

- The identity behind `EVALRY_TOKEN` needs the author role (to request and poll runs) and the approver role (to record the
  decision) on this project only. Keep its token in the CI secret store and treat it like a deploy key. The operator creates
  it ([operator-runbook.md](operator-runbook.md), section 6).
- Every pull request that needs fresh results needs a fresh `GENERATOR` label (the script uses the commit). Using the same label
  for the same suite version returns `409` once recorded: that is the platform refusing to overwrite an immutable run.
- The platform decides staging or production, not the job.

## 5. Make it required, safely

1. **Rehearse first.** Run it on every pull request for two to three weeks without making it required. Look at every failure:
   real, task bug, or flaky. Fix tasks until failures are mostly real.
2. **Keep it fast.** Aim for under 10 minutes at the 90th percentile. A small smoke suite (10 to 25 tasks, one sample) is
   enough for a hard gate; run bigger suites nightly.
3. **Make the job required** in branch protection only after the rehearsal looks clean, and only for the repository the pilot
   covers.
4. **Decide what a failure of the platform means.** Shape A has no platform dependency at gate time. Shape B fails closed: if
   the platform is down the job exits `2` and the pull request is blocked. Agree who may approve a manual merge and how it is
   recorded (the reason and an expiry in the pull request), because the automated outage bypass is not built.
5. **Do not let the gate drift.** The gate rule names an approved suite version. Changing a suite means a new version, and
   changing the version in the rule goes through review like code.
6. **Watch the evidence.** Keep the gate artifacts, and read the health report weekly.

## 6. Checklist before you switch it on

- [ ] The suite version in the rule matches the one in the workflow, and the tasks pass their reference and fail their stub.
- [ ] The workflow only passes the prompt, never the tests, to your system.
- [ ] Pull requests run in `staging`; only the protected branch holds the signing key.
- [ ] Changes to the workflow, the gate rule and the tasks require review.
- [ ] Someone owns triage of failures this week, and a dispute path is agreed with the approvers.
- [ ] Docker is available on the runner, and the job has a time limit.
- [ ] The team knows the manual-merge procedure for an outage.
