"""Roles, tenant and project scoping, and the one data-access layer every request goes through (plan §2, §9).

Isolation is structural: every record is keyed by (tenant, project), and a caller can only build keys from their own tenant,
so another tenant's projects do not exist for them. A project the caller cannot see and one that does not exist produce the
same error, so ids cannot be probed. Roles are checked at a single choke point (``_resolve``) and every denial, grant and
write is recorded in the audit log. Storage sits behind ``Backend``: the in-memory backend for tests and local use, the
Postgres backend (tenant_id in every key, row-level security as a second layer) for deployment.
"""

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from evalry.audit import AuditLog
from evalry.backend import Backend, MemoryBackend
from evalry.gates import GateDecision
from evalry.schemas import RunRecord


class Role(IntEnum):
    VIEWER = 1
    AUTHOR = 2
    APPROVER = 3
    ADMIN = 4


class Action(StrEnum):
    READ = "read"  # see runs and decisions
    SUBMIT = "submit"  # start runs, record results
    APPROVE = "approve"  # record gate decisions
    ADMIN = "admin"  # manage members


_NEEDS: dict[Action, Role] = {
    Action.READ: Role.VIEWER,
    Action.SUBMIT: Role.AUTHOR,
    Action.APPROVE: Role.APPROVER,
    Action.ADMIN: Role.ADMIN,
}


class AccessError(Exception):
    """Base class: the request was refused."""


class NotFound(AccessError):
    """The project or record does not exist for this caller (which includes: it belongs to another tenant)."""


class PermissionDenied(AccessError):
    """The caller can see the project but their role does not allow this action."""


class Conflict(AccessError):
    """Results and decisions are immutable: a record with this id already exists."""


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``tenant_id`` comes from the verified identity token, never from the request body."""

    subject: str
    tenant_id: str


def _missing(project_id: str) -> NotFound:
    return NotFound(f"Project {project_id!r} was not found. Check the id, or ask a project admin for access.")


class Store:
    """Owns all state. Handlers never touch it directly: they get a ``ScopedStore`` from ``view``."""

    def __init__(self, audit: AuditLog, backend: Backend | None = None) -> None:
        self._audit = audit
        self._b: Backend = backend or MemoryBackend()

    def bootstrap_tenant(self, tenant_id: str, admin_subject: str, *, operator: str) -> None:
        """Operator action (not reachable from a request): create a tenant with its first admin."""
        if not self._b.create_tenant(tenant_id, admin_subject):
            raise Conflict(f"Tenant {tenant_id!r} already exists.")
        self._audit.append(
            operator, "tenant.created", tenant_id, source="operator", tenant_id=tenant_id,
            details={"first_admin": admin_subject},
        )  # fmt: skip

    def view(self, principal: Principal) -> "ScopedStore":
        return ScopedStore(self, principal)


class ScopedStore:
    def __init__(self, store: Store, principal: Principal) -> None:
        self._s = store
        self._b = store._b
        self._p = principal

    @property
    def tenant_id(self) -> str:
        return self._p.tenant_id

    @property
    def subject(self) -> str:
        return self._p.subject

    def require(self, project_id: str, action: Action) -> None:
        """Fail fast, before any work starts, if the caller may not ``action`` this project."""
        self._resolve(project_id, action)

    def holds_at_least(self, project_id: str, subject: str, role: Role) -> bool:
        """Does another subject hold ``role`` or higher here? For checking a named approver; needs the approve action."""
        self._resolve(project_id, Action.APPROVE)
        if self._b.is_tenant_admin(self._p.tenant_id, subject):
            return True
        held = self._b.member_role(self._p.tenant_id, project_id, subject)
        return held is not None and held >= role

    # -- the choke point -------------------------------------------------------------------------------------------

    def role_of(self, project_id: str) -> Role | None:
        t, who = self._p.tenant_id, self._p.subject
        if not self._b.project_exists(t, project_id):
            return None
        if self._b.is_tenant_admin(t, who):
            return Role.ADMIN
        held = self._b.member_role(t, project_id, who)
        return None if held is None else Role(held)

    def _resolve(self, project_id: str, action: Action) -> None:
        """Raise unless the caller may do ``action`` on the project; audit every refusal."""
        role = self.role_of(project_id)
        if role is None:
            self._deny(project_id, action, "no access or no such project")
            raise _missing(project_id)
        if role < _NEEDS[action]:
            self._deny(project_id, action, f"role {role.name.lower()} cannot {action.value}")
            raise PermissionDenied(
                f"Your role on project {project_id!r} is {role.name.lower()}; {action.value} needs "
                f"{_NEEDS[action].name.lower()} or higher. Ask a project admin."
            )

    def _deny(self, project_id: str, action: Action, why: str) -> None:
        self._s._audit.append(
            self._p.subject, "access.denied", project_id, source="access", tenant_id=self._p.tenant_id,
            project_id=project_id, details={"action": action.value, "why": why},
        )  # fmt: skip

    def _record(self, action: str, project_id: str, target: str, **details: str) -> None:
        self._s._audit.append(
            self._p.subject, action, target, source="access", tenant_id=self._p.tenant_id,
            project_id=project_id, details=details,
        )  # fmt: skip

    # -- projects and members --------------------------------------------------------------------------------------

    def create_project(self, project_id: str) -> None:
        t = self._p.tenant_id
        if not self._b.is_tenant_admin(t, self._p.subject):
            self._deny(project_id, Action.ADMIN, "only tenant admins create projects")
            raise PermissionDenied("Only tenant admins can create projects. Ask a tenant admin.")
        if not self._b.create_project(t, project_id):
            raise Conflict(f"Project {project_id!r} already exists in your tenant.")
        self._record("project.created", project_id, project_id)

    def grant(self, project_id: str, subject: str, role: Role) -> None:
        self._resolve(project_id, Action.ADMIN)
        self._b.set_member(self._p.tenant_id, project_id, subject, int(role))
        self._record("member.granted", project_id, subject, role=role.name.lower())

    def revoke(self, project_id: str, subject: str) -> None:
        self._resolve(project_id, Action.ADMIN)
        removed = self._b.remove_member(self._p.tenant_id, project_id, subject)
        if removed is not None:
            self._record("member.revoked", project_id, subject, role=Role(removed).name.lower())

    # -- runs and decisions ----------------------------------------------------------------------------------------

    def put_run(self, project_id: str, record: RunRecord) -> None:
        self._resolve(project_id, Action.SUBMIT)
        if not self._b.put_run(self._p.tenant_id, project_id, record):
            raise Conflict(
                f"Run {record.run_id!r} already exists; run records are immutable. Use a new run id."
            )
        self._record("run.recorded", project_id, record.run_id, suite_version=record.suite_version)

    def get_run(self, project_id: str, run_id: str) -> RunRecord:
        self._resolve(project_id, Action.READ)
        found = self._b.get_run(self._p.tenant_id, project_id, run_id)
        if found is None:
            raise NotFound(f"Run {run_id!r} was not found in project {project_id!r}.")
        return found

    def list_runs(self, project_id: str) -> list[str]:
        self._resolve(project_id, Action.READ)
        return self._b.list_runs(self._p.tenant_id, project_id)

    def put_decision(self, project_id: str, decision: GateDecision) -> None:
        self._resolve(project_id, Action.APPROVE)
        if self._b.get_run(self._p.tenant_id, project_id, decision.run_id) is None:
            raise NotFound(
                f"Run {decision.run_id!r} was not found in project {project_id!r}; a decision must reference a stored run."
            )
        if not self._b.put_decision(self._p.tenant_id, project_id, decision):
            raise Conflict(f"Decision {decision.decision_id!r} already exists; gate decisions are immutable.")
        self._record("decision.recorded", project_id, decision.decision_id, outcome=decision.outcome)

    def get_decision(self, project_id: str, decision_id: str) -> GateDecision:
        self._resolve(project_id, Action.READ)
        found = self._b.get_decision(self._p.tenant_id, project_id, decision_id)
        if found is None:
            raise NotFound(f"Decision {decision_id!r} was not found in project {project_id!r}.")
        return found
