"""Storage interface behind the access layer. Every method takes the tenant explicitly: there is no call that reads across
tenants, so a backend cannot be asked a cross-tenant question. Roles cross this boundary as small ints (viewer=1 ... admin=4).
"""

import hashlib
import json
import threading
from typing import Protocol

from evalry.gates.hard import GateDecision
from evalry.schemas import RunRecord, Task


def suite_hash(tasks: list[Task]) -> str:
    """Hash of the exact suite content, in order. A gate can pin this instead of trusting a version label."""
    return hashlib.sha256(
        json.dumps([t.model_dump(mode="json") for t in tasks], sort_keys=True).encode()
    ).hexdigest()


class Backend(Protocol):
    def create_tenant(self, tenant_id: str, admin_subject: str) -> bool:
        """False if the tenant already exists."""
        ...

    def is_tenant_admin(self, tenant_id: str, subject: str) -> bool: ...
    def project_exists(self, tenant_id: str, project_id: str) -> bool: ...
    def create_project(self, tenant_id: str, project_id: str) -> bool:
        """False if it already exists."""
        ...

    def member_role(self, tenant_id: str, project_id: str, subject: str) -> int | None: ...
    def set_member(self, tenant_id: str, project_id: str, subject: str, role: int) -> None: ...
    def remove_member(self, tenant_id: str, project_id: str, subject: str) -> int | None:
        """The role that was removed, or None if the subject was not a member."""
        ...

    def put_run(self, tenant_id: str, project_id: str, record: RunRecord) -> bool:
        """False if a run with this id already exists (runs are immutable)."""
        ...

    def get_run(self, tenant_id: str, project_id: str, run_id: str) -> RunRecord | None: ...
    def list_runs(self, tenant_id: str, project_id: str) -> list[str]: ...
    def put_decision(self, tenant_id: str, project_id: str, decision: GateDecision) -> bool: ...
    def put_suite(
        self, tenant_id: str, project_id: str, version: str, tasks: list[Task], created_by: str
    ) -> str | None:
        """Store an immutable suite version and return its content hash, or None if the version already exists."""
        ...

    def get_suite(self, tenant_id: str, project_id: str, version: str) -> list[Task] | None: ...
    def list_suites(self, tenant_id: str, project_id: str) -> list[str]: ...
    def get_decision(self, tenant_id: str, project_id: str, decision_id: str) -> GateDecision | None: ...


class MemoryBackend:
    """For tests, local development and fake-provider environments. State is lost on restart."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tenants: dict[str, set[str]] = {}
        self._projects: set[tuple[str, str]] = set()
        self._members: dict[tuple[str, str], dict[str, int]] = {}
        self._runs: dict[tuple[str, str, str], RunRecord] = {}
        self._decisions: dict[tuple[str, str, str], GateDecision] = {}
        self._suites: dict[tuple[str, str, str], list[Task]] = {}

    def create_tenant(self, tenant_id: str, admin_subject: str) -> bool:
        with self._lock:
            if tenant_id in self._tenants:
                return False
            self._tenants[tenant_id] = {admin_subject}
            return True

    def is_tenant_admin(self, tenant_id: str, subject: str) -> bool:
        with self._lock:
            return subject in self._tenants.get(tenant_id, ())

    def project_exists(self, tenant_id: str, project_id: str) -> bool:
        with self._lock:
            return (tenant_id, project_id) in self._projects

    def create_project(self, tenant_id: str, project_id: str) -> bool:
        with self._lock:
            if (tenant_id, project_id) in self._projects:
                return False
            self._projects.add((tenant_id, project_id))
            self._members[(tenant_id, project_id)] = {}
            return True

    def member_role(self, tenant_id: str, project_id: str, subject: str) -> int | None:
        with self._lock:
            return self._members.get((tenant_id, project_id), {}).get(subject)

    def set_member(self, tenant_id: str, project_id: str, subject: str, role: int) -> None:
        with self._lock:
            self._members[(tenant_id, project_id)][subject] = role

    def remove_member(self, tenant_id: str, project_id: str, subject: str) -> int | None:
        with self._lock:
            return self._members[(tenant_id, project_id)].pop(subject, None)

    def put_run(self, tenant_id: str, project_id: str, record: RunRecord) -> bool:
        with self._lock:
            key = (tenant_id, project_id, record.run_id)
            if key in self._runs:
                return False
            self._runs[key] = record
            return True

    def get_run(self, tenant_id: str, project_id: str, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get((tenant_id, project_id, run_id))

    def list_runs(self, tenant_id: str, project_id: str) -> list[str]:
        with self._lock:
            return sorted(r for (t, p, r) in self._runs if t == tenant_id and p == project_id)

    def put_decision(self, tenant_id: str, project_id: str, decision: GateDecision) -> bool:
        with self._lock:
            key = (tenant_id, project_id, decision.decision_id)
            if key in self._decisions:
                return False
            self._decisions[key] = decision
            return True

    def get_decision(self, tenant_id: str, project_id: str, decision_id: str) -> GateDecision | None:
        with self._lock:
            return self._decisions.get((tenant_id, project_id, decision_id))

    def put_suite(
        self, tenant_id: str, project_id: str, version: str, tasks: list[Task], created_by: str
    ) -> str | None:
        with self._lock:
            key = (tenant_id, project_id, version)
            if key in self._suites:
                return None
            self._suites[key] = list(tasks)
            return suite_hash(tasks)

    def get_suite(self, tenant_id: str, project_id: str, version: str) -> list[Task] | None:
        with self._lock:
            found = self._suites.get((tenant_id, project_id, version))
            return None if found is None else list(found)

    def list_suites(self, tenant_id: str, project_id: str) -> list[str]:
        with self._lock:
            return sorted(v for (t, p, v) in self._suites if t == tenant_id and p == project_id)
