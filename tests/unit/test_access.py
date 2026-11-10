from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest

from evalry.access import Conflict, NotFound, PermissionDenied, Principal, Role, ScopedStore, Store
from evalry.audit import AuditLog
from evalry.backend import Backend
from evalry.gates import GateDecision
from evalry.schemas import RunRecord

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def run(run_id: str = "r1") -> RunRecord:
    return RunRecord(run_id=run_id, suite_version="s1", started_at=NOW, results=[])


def decision(decision_id: str = "d1", run_id: str = "r1") -> GateDecision:
    return GateDecision(
        decision_id=decision_id, gate="smoke", rule_hash="h", run_id=run_id, suite_version="s1", environment="production",
        outcome="pass", reasons=[], waived=[], pass_rate=1.0, samples=1, actor="ci", decided_at=NOW,
    )  # fmt: skip


class World:
    """Two tenants, each with a project called 'app' and a full set of roles."""

    def __init__(self, tmp_path: Path, backend: Backend) -> None:
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.store = Store(self.audit, backend)
        for tenant in ("acme", "globex"):
            self.store.bootstrap_tenant(tenant, f"{tenant}-admin", operator="ops")
            self.as_(f"{tenant}-admin", tenant).create_project("app")
        self.grant_all()

    def as_(self, subject: str, tenant: str) -> ScopedStore:
        return self.store.view(Principal(subject, tenant))

    def grant_all(self) -> None:
        for tenant in ("acme", "globex"):
            admin = self.as_(f"{tenant}-admin", tenant)
            for role in Role:
                admin.grant("app", f"{tenant}-{role.name.lower()}", role)


@pytest.fixture
def w(tmp_path: Path, backend: Backend) -> World:
    return World(tmp_path, backend)


def test_role_matrix(w: World) -> None:
    n = count()
    w.as_("acme-author", "acme").put_run("app", run("r1"))  # decisions must reference a stored run
    actions: dict[str, Callable[[ScopedStore], object]] = {
        "read": lambda s: s.list_runs("app"),
        "submit": lambda s: s.put_run("app", run(f"r-{next(n)}")),
        "approve": lambda s: s.put_decision("app", decision(f"d-{next(n)}")),
        "admin": lambda s: s.grant("app", "someone", Role.VIEWER),
    }
    needs = {"read": Role.VIEWER, "submit": Role.AUTHOR, "approve": Role.APPROVER, "admin": Role.ADMIN}
    for role in Role:
        for name, act in actions.items():
            scoped = w.as_(f"acme-{role.name.lower()}", "acme")
            if role >= needs[name]:
                act(scoped)
            else:
                with pytest.raises(PermissionDenied):
                    act(scoped)


def test_cross_tenant_reads_fail_even_for_admins_and_with_the_same_project_id(w: World) -> None:
    w.as_("acme-author", "acme").put_run("app", run("secret-run"))
    w.as_("acme-approver", "acme").put_decision("app", decision("secret-decision", "secret-run"))
    for attacker in ("globex-admin", "globex-viewer", "globex-author", "globex-approver"):
        s = w.as_(attacker, "globex")
        with pytest.raises(NotFound):
            s.get_run(
                "app", "secret-run"
            )  # same project id exists in their own tenant: still not acme's data
        with pytest.raises(NotFound):
            s.get_decision("app", "secret-decision")
        assert s.list_runs("app") == []
    # a subject name collision does not carry roles across tenants
    with pytest.raises(NotFound):
        w.as_("acme-admin", "globex").list_runs("app")


def test_cross_tenant_writes_and_grants_fail(w: World) -> None:
    attacker = w.as_("globex-admin", "globex")
    other = w.as_("acme-admin", "acme")
    attacker.put_run("app", run("mine"))
    assert other.list_runs("app") == []  # landed in globex only
    with pytest.raises(NotFound):
        w.as_("globex-admin", "globex").grant("acme-only-project", "x", Role.ADMIN)


def test_missing_and_foreign_projects_are_indistinguishable(w: World) -> None:
    w.as_("acme-admin", "acme").create_project("only-acme")
    s = w.as_("globex-admin", "globex")
    with pytest.raises(NotFound) as foreign:
        s.list_runs("only-acme")
    with pytest.raises(NotFound) as missing:
        s.list_runs("never-existed")
    assert str(foreign.value).replace("only-acme", "X") == str(missing.value).replace("never-existed", "X")


def test_a_tenant_member_with_no_role_on_a_project_cannot_see_it(w: World) -> None:
    w.as_("acme-admin", "acme").create_project("private")
    with pytest.raises(NotFound):
        w.as_("acme-viewer", "acme").list_runs("private")  # has a role on 'app' only
    assert (
        w.as_("acme-admin", "acme").list_runs("private") == []
    )  # tenant admins see all projects of their tenant


def test_results_and_decisions_are_immutable(w: World) -> None:
    a = w.as_("acme-author", "acme")
    a.put_run("app", run("r1"))
    with pytest.raises(Conflict):
        a.put_run("app", run("r1"))
    ap = w.as_("acme-approver", "acme")
    ap.put_decision("app", decision("d1"))
    with pytest.raises(Conflict):
        ap.put_decision("app", decision("d1"))
    assert w.as_("acme-viewer", "acme").get_run("app", "r1").run_id == "r1"
    with pytest.raises(NotFound):
        w.as_("acme-viewer", "acme").get_run("app", "nope")


def test_revoke_takes_effect_and_only_admins_manage_members(w: World) -> None:
    admin = w.as_("acme-admin", "acme")
    admin.revoke("app", "acme-author")
    with pytest.raises(NotFound):
        w.as_("acme-author", "acme").put_run("app", run("x"))
    with pytest.raises(PermissionDenied):
        w.as_("acme-approver", "acme").grant("app", "friend", Role.ADMIN)
    with pytest.raises(PermissionDenied):
        w.as_("acme-approver", "acme").create_project("sneaky")  # only tenant admins create projects
    admin.revoke("app", "never-a-member")  # revoking a non-member is a no-op, not an error


def test_tenant_and_project_creation_conflicts(w: World) -> None:
    with pytest.raises(Conflict):
        w.store.bootstrap_tenant("acme", "someone", operator="ops")
    with pytest.raises(Conflict):
        w.as_("acme-admin", "acme").create_project("app")
    w.as_("globex-admin", "globex").create_project("shared-name")
    w.as_("acme-admin", "acme").create_project("shared-name")  # same name in another tenant is fine


def test_audit_records_denials_grants_and_writes_with_tenant_and_project(w: World) -> None:
    with pytest.raises(NotFound):
        w.as_("globex-viewer", "globex").list_runs("not-mine")
    with pytest.raises(PermissionDenied):
        w.as_("acme-viewer", "acme").put_run("app", run("r9"))
    w.as_("acme-author", "acme").put_run("app", run("r9"))
    events = w.audit.events()
    denied = [e for e in events if e.action == "access.denied"]
    assert [(e.actor, e.tenant_id, e.details["action"]) for e in denied] == [
        ("globex-viewer", "globex", "read"),
        ("acme-viewer", "acme", "submit"),
    ]
    assert any(e.action == "run.recorded" and e.tenant_id == "acme" and e.project_id == "app" for e in events)
    assert {e.action for e in events} >= {
        "tenant.created",
        "project.created",
        "member.granted",
        "run.recorded",
    }
    assert w.audit.verify() == []
