from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evalry.access import NotFound, PermissionDenied, Principal, Role, ScopedStore, Store
from evalry.audit import AuditLog
from evalry.gates import GateDecision, GateException, HardGateRule
from evalry.generation import FakeGenerator
from evalry.jobs import Job, JobQueue, ProjectLimits, State
from evalry.schemas import SampleResult, Task, load_task
from evalry.service import gate_run, submit_suite
from evalry.worker import submit_run, work

GOLDEN = Path(__file__).parent.parent / "golden"
GOOD = (GOLDEN / "solutions/good.txt").read_text()
RULE = HardGateRule(name="smoke", suite_version="s1", grader_version="2")


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.store = Store(self.audit)
        for tenant in ("acme", "globex"):
            self.store.bootstrap_tenant(tenant, f"{tenant}-admin", operator="ops")
            admin = self.as_(f"{tenant}-admin", tenant)
            admin.create_project("app")
            for role in Role:
                admin.grant("app", f"{tenant}-{role.name.lower()}", role)
        self.task = load_task(GOLDEN / "fizzbuzz.yaml")

    def as_(self, subject: str, tenant: str) -> ScopedStore:
        return self.store.view(Principal(subject, tenant))

    def submit(self, subject: str = "acme-author", tenant: str = "acme", reply: str = GOOD) -> str:
        gen = FakeGenerator([reply])
        record = submit_suite(
            self.as_(subject, tenant),
            "app",
            [self.task],
            lambda t, i: gen,
            samples=1,
            suite_version="s1",
            audit=self.audit,
        )
        return record.run_id


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_author_runs_a_suite_and_the_record_is_stored_and_audited_under_their_tenant(env: Env) -> None:
    run_id = env.submit()
    assert env.as_("acme-viewer", "acme").get_run("app", run_id).results[0].composite == 1.0
    events = env.audit.events()
    run_events = [e for e in events if e.action in ("run.submitted", "job.completed", "run.recorded")]
    assert {e.tenant_id for e in run_events} == {"acme"} and {e.project_id for e in run_events} == {"app"}
    assert next(e for e in run_events if e.action == "run.submitted").actor == "acme-author"
    assert env.audit.verify() == []


def test_viewer_cannot_submit_and_nothing_runs(env: Env) -> None:
    gen = FakeGenerator([GOOD])
    with pytest.raises(PermissionDenied):
        submit_suite(
            env.as_("acme-viewer", "acme"),
            "app",
            [env.task],
            lambda t, i: gen,
            samples=1,
            suite_version="s1",
            audit=env.audit,
        )
    assert gen.prompts == []  # refused before any generation or container work
    assert env.as_("acme-admin", "acme").list_runs("app") == []


def test_other_tenant_cannot_submit_to_or_gate_or_see_a_run(env: Env) -> None:
    run_id = env.submit()
    with pytest.raises(NotFound):
        gate_run(
            env.as_("globex-approver", "globex"),
            "app",
            run_id,
            RULE,
            environment="dev",
            key=None,
            audit=env.audit,
        )
    gen = FakeGenerator([GOOD])
    with pytest.raises(NotFound):
        submit_suite(
            env.as_("globex-author", "acme-project-they-guess".replace("acme-project-they-guess", "globex")), "nope",
            [env.task], lambda t, i: gen, samples=1, suite_version="s1", audit=env.audit,
        )  # fmt: skip
    assert gen.prompts == []


def test_approver_gates_a_stored_run_and_the_decision_is_stored(env: Env) -> None:
    run_id = env.submit()
    d = gate_run(
        env.as_("acme-approver", "acme"),
        "app",
        run_id,
        RULE,
        environment="production",
        key=b"k",
        audit=env.audit,
    )
    assert d.outcome == "pass" and d.actor == "acme-approver"
    assert env.as_("acme-viewer", "acme").get_decision("app", d.decision_id) == d
    assert any(e.action == "decision.recorded" and e.tenant_id == "acme" for e in env.audit.events())
    assert any(e.action == "gate.decided" and e.tenant_id == "acme" for e in env.audit.events())


def test_author_cannot_gate(env: Env) -> None:
    run_id = env.submit()
    with pytest.raises(PermissionDenied):
        gate_run(
            env.as_("acme-author", "acme"), "app", run_id, RULE, environment="dev", key=None, audit=env.audit
        )


def test_gating_an_unknown_run_is_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        gate_run(
            env.as_("acme-approver", "acme"),
            "app",
            "nope",
            RULE,
            environment="dev",
            key=None,
            audit=env.audit,
        )


def exception(approver: str) -> GateException:
    return GateException(
        task_id="fizzbuzz",
        approver=approver,
        reason="known flake",
        expires_at=datetime.now(UTC) + timedelta(days=3),
    )


def test_exception_approver_must_hold_the_approver_role(env: Env) -> None:
    run_id = env.submit(reply="nope")  # fails, so an exception would matter

    def gate(exc: GateException) -> GateDecision:
        return gate_run(
            env.as_("acme-approver", "acme"), "app", run_id, RULE, environment="dev", key=None, audit=env.audit,
            exceptions=[exc],
        )  # fmt: skip

    for bad in ("acme-viewer", "acme-author", "someone-not-a-member", "globex-approver"):
        with pytest.raises(PermissionDenied, match="does not hold the approver role"):
            gate(exception(bad))
    assert [e.details["named_approver"] for e in env.audit.events() if e.action == "gate.exception.rejected"] == [
        "acme-viewer", "acme-author", "someone-not-a-member", "globex-approver",
    ]  # fmt: skip
    for ok in ("acme-approver", "acme-admin"):
        # waiving the only failing task leaves nothing gated, so the gate still blocks: the exception was accepted, not abused
        assert gate(exception(ok)).outcome == "block"
        run_id = env.submit(reply="nope")


def test_job_keys_and_budgets_do_not_cross_tenants() -> None:
    tasks = [Task(id="t", language="python", prompt="p", tests="t")]
    q = JobQueue(
        {("acme", "app"): ProjectLimits(budget_usd=1.0), ("globex", "app"): ProjectLimits(budget_usd=100.0)}
    )
    a = submit_run(
        q, tasks, project_id="app", suite_version="s1", generator_hash="g", samples=2, tenant_id="acme"
    )
    g = submit_run(
        q, tasks, project_id="app", suite_version="s1", generator_hash="g", samples=2, tenant_id="globex"
    )
    assert {j.key for j in a}.isdisjoint({j.key for j in g}) and len(
        q.jobs()
    ) == 4  # same names, separate jobs

    def costly(job: Job, task: Task) -> SampleResult:
        return SampleResult(
            task_id=task.id,
            task_hash="h",
            sample=job.sample,
            generator_model="m",
            code="c",
            scores=[],
            composite=1,
            cost_usd=1.0,
        )

    work(q, {"t": tasks[0]}, costly)
    assert (
        q.spent("app", "acme") == 1.0 and q.spent("app", "globex") == 2.0
    )  # acme hit its $1 budget, globex did not
    assert [j.state for j in a].count(State.QUEUED) == 1 and all(j.state is State.DONE for j in g)
