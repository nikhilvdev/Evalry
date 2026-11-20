# Evalry pilot onboarding

This folder is for the first team that gates real pull requests with Evalry, and for the person who runs the platform for them.

| Read this | If you are | It covers |
| --- | --- | --- |
| This page | Everyone | What the pilot is, what must be true before it starts, who does what, how success is judged |
| [operator-runbook.md](operator-runbook.md) | The platform operator | Standing the platform up, creating the tenant and the team's project, running workers, day-to-day checks, troubleshooting |
| [team-guide.md](team-guide.md) | The pilot team's engineers | Writing tasks, versioning suites, running evals, reading results, disputing a failed gate |
| [ci-gate.md](ci-gate.md) | The pilot team's CI owner | Putting the hard gate on a pull request |

General usage of every command is in [../usage.md](../usage.md). These pages add the pilot-specific steps and decisions.

## What the pilot is

One team (a Python-heavy platform or backend team) uses Evalry to **block a pull request when AI-generated code fails
deterministic tests**: hidden unit tests that the generating model never sees, run in an isolated container, with a signed,
audited decision at the end.

Success means the team gates a real pull request in CI, trusts the result, and tells us what is missing.

### What the gate decides, and what it does not

- The gate is a **hard gate on deterministic graders only**: the hidden tests must pass at or above a floor. It does not use
  the LLM judge, and it does not do statistical regression checks. Those need a calibrated judge and 100 or more tasks.
- Only results graded with `rpc` isolation can gate (the solution and the hidden tests run in separate containers). Anything
  else is advisory and is refused by the gate.
- Repository tasks (tier 3) and agent tasks (tier 4) are **advisory only** in the pilot: they run, they are recorded, and they
  can never block a pull request.
- Tasks are function-level Python. A task whose tests need classes, callbacks or generators fails closed (score 0). See the
  team guide for how to write tasks that work.
- A gate result says "these tasks passed". It does not say the generated code is correct beyond them, or safe for production.
  It is not a replacement for human review of high-risk code.

## Before the pilot starts: blocking items

These need a named person. Nothing here can be defaulted by the platform, and the pilot should not start until each has an
answer written next to it.

| # | Item | Owner | Answer |
| --- | --- | --- | --- |
| 1 | **Pilot team and champion**: the team, and one person on it who will push adoption and report problems | | |
| 2 | **Sandbox security sign-off**: the internal sandbox security review is complete, and the sign-off owner has accepted or fixed each finding | | |
| 3 | **Runner node**: a dedicated machine for the sandbox containers, separate from the API, database and any developer workloads | | |
| 4 | **Identity provider**: an OIDC provider that issues signed tokens with a tenant claim, and the issuer, audience and JWKS URL | | |
| 5 | **Model and budget**: which model generates solutions, its prices per million tokens, a spend limit set **at the provider**, and who approves spend | | |
| 6 | **Data classification**: which sensitivity levels may be sent to the model provider (default `public` and `internal`) | | |
| 7 | **Gate approvers**: who holds the approver role, and who may name an exception | | |
| 8 | **Outage bypass**: what the team does if the platform is down and a release is urgent (see "When the gate is wrong or the platform is down") | | |

Until 2 is done, run the pilot only against non-sensitive code and do not widen it.

## Roles

| Role | Who | Can do |
| --- | --- | --- |
| Operator | Platform owner | Create the tenant and the first admin, run the services |
| Tenant admin / project admin | The champion | Create the project, grant roles |
| Author | Engineers who write tasks | Create suites (hidden tests included), start runs |
| Approver | Tech lead, security contact | Record gate decisions, approve exceptions, resolve disputes |
| Viewer | Anyone who needs to see results | Read runs, decisions and the health report |

A person can hold more than one. An approver who opened a dispute cannot resolve it.

## Suggested timeline

Adjust to the team's capacity.

| Week | Goal |
| --- | --- |
| 0 | Blocking items answered. Platform up. Tenant and project created. Roles granted. |
| 1 | The team writes its first 5 to 10 tasks from real bugs or specs and runs them locally. |
| 2 | Suite `v1` created on the platform. First end-to-end run with the real model. Cost per run measured. |
| 3 | Gate rule agreed. The workflow runs on pull requests as a **rehearsal** (staging): it reports but nobody is blocked. |
| 4 to 5 | Tasks grow to 25 to 50. Flaky or wrong tasks fixed. Rehearsal results reviewed weekly. |
| 6 | The gate becomes official on one repository: `production` environment, signed decisions. |
| 7 onward | Review the success measures below, collect gaps, decide whether to widen. |

## Success measures

Proposed targets, to agree with the team in week 0 and review at week 7.

| Measure | Target |
| --- | --- |
| The team gates at least one real pull request in CI | yes |
| Gate failures the team confirms were real problems | 80% or more |
| Runs that finish without a platform error | 99% or more |
| Smoke run time on a pull request (p90) | under 10 minutes |
| Spend per run within its estimate | within 20% |
| Time to add and validate a new task | under 30 minutes |

Measure from the health report (`evalry health`) and from the team's own tally of confirmed-real versus disputed failures.

## When the gate is wrong or the platform is down

- **A task is wrong** (a bad reference, a flaky test): fix the task in a new suite version, re-run, and use a dispute (team
  guide, section 8) so the correction is recorded.
- **One task must be waived for now**: an approver grants an exception with a reason and an expiry (at most 30 days through a
  dispute). It is named in the audit log.
- **The platform is down**: the automated outage bypass is not built. Agree in advance who may approve a manual merge, record
  the reason and an expiry in the pull request, and tell the operator so it can be reviewed.

## Known limits during the pilot

- Run ids are derived from tenant, project, suite version and the generator name. Re-running an already recorded run needs a new
  generator name.
- There is no per-project budget stop on the platform path yet. Set a spend limit at the model provider.
- SSO is OIDC only; there is no SAML and no automatic mapping of groups to roles. Roles are granted by an admin.
- There is no backup or restore procedure yet. Treat pilot data as recoverable by re-running, not as a system of record.
- Time to result per run is not recorded. Measure it from the CI job.
