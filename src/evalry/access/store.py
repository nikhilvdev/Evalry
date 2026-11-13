"""Roles, tenant and project scoping, and the one data-access layer every request goes through (plan §2, §9).

Isolation is structural: every record is keyed by (tenant, project), and a caller can only build keys from their own tenant,
so another tenant's projects do not exist for them. A project the caller cannot see and one that does not exist produce the
same error, so ids cannot be probed. Roles are checked at a single choke point (``_resolve``) and every denial, grant and
write is recorded in the audit log. Storage sits behind ``Backend``: the in-memory backend for tests and local use, the
Postgres backend (tenant_id in every key, row-level security as a second layer) for deployment.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import IntEnum, StrEnum

from evalry import disputes, health, labeling, monitoring
from evalry.audit.chain import AuditLog
from evalry.gates.hard import GateDecision, GateException
from evalry.judge.calibration import CalibrationRecord, Labeled, passing_versions
from evalry.schemas import RunRecord, Task
from evalry.storage.backend import Backend, MemoryBackend
from evalry.storage.objects import ObjectError, ObjectStore


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
    """Results, decisions and suite versions are immutable: a record with this id already exists."""


@dataclass(frozen=True)
class Principal:
    """Who is calling. ``tenant_id`` comes from the verified identity token, never from the request body."""

    subject: str
    tenant_id: str


def _missing(project_id: str) -> NotFound:
    return NotFound(f"Project {project_id!r} was not found. Check the id, or ask a project admin for access.")


class Store:
    """Owns all state. Handlers never touch it directly: they get a ``ScopedStore`` from ``view``."""

    def __init__(
        self, audit: AuditLog, backend: Backend | None = None, objects: ObjectStore | None = None
    ) -> None:
        self._audit = audit
        self._b: Backend = backend or MemoryBackend()
        self._objects = objects  # when set, solution code lives here and the run row keeps only its digest

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
        stored = self._offload(project_id, record)
        if not self._b.put_run(self._p.tenant_id, project_id, stored):
            raise Conflict(
                f"Run {record.run_id!r} already exists; run records are immutable. Use a new run id."
            )
        self._record("run.recorded", project_id, record.run_id, suite_version=record.suite_version)

    def get_run(self, project_id: str, run_id: str) -> RunRecord:
        self._resolve(project_id, Action.READ)
        found = self._b.get_run(self._p.tenant_id, project_id, run_id)
        if found is None:
            raise NotFound(f"Run {run_id!r} was not found in project {project_id!r}.")
        return self._rehydrate(project_id, found)

    def _offload(self, project_id: str, record: RunRecord) -> RunRecord:
        """Move each sample's code to object storage. Written before the row, so a failure leaves only unreferenced blobs."""
        if self._s._objects is None:
            return record
        t, objects = self._p.tenant_id, self._s._objects
        return record.model_copy(
            update={
                "results": [
                    r.model_copy(
                        update={"code": "", "code_digest": objects.put(t, project_id, r.code.encode())}
                    )
                    for r in record.results
                ]
            }
        )

    def _rehydrate(self, project_id: str, record: RunRecord) -> RunRecord:
        """Put code back from object storage. The caller's READ role was already checked by ``get_run``."""
        if all(r.code_digest is None for r in record.results):
            return record
        if self._s._objects is None:
            raise ObjectError("This run's code is in object storage, but no object store is configured.")
        t, objects = self._p.tenant_id, self._s._objects
        results = []
        for r in record.results:
            if r.code_digest is None:
                results.append(r)
                continue
            blob = objects.get(t, project_id, r.code_digest)
            if blob is None:
                raise ObjectError(
                    f"Code {r.code_digest} for run {record.run_id!r} is missing from object storage."
                )
            results.append(r.model_copy(update={"code": blob.decode()}))
        return record.model_copy(update={"results": results})

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

    # -- suites ----------------------------------------------------------------------------------------------------

    def put_suite(self, project_id: str, version: str, tasks: list[Task]) -> str:
        """Create an immutable suite version (author or higher). Returns the content hash a gate can pin."""
        self._resolve(project_id, Action.SUBMIT)
        if not tasks:
            raise ValueError("A suite needs at least one task.")
        if len({t.id for t in tasks}) != len(tasks):
            raise ValueError("Task ids in a suite must be unique.")
        digest = self._b.put_suite(self._p.tenant_id, project_id, version, tasks, self._p.subject)
        if digest is None:
            raise Conflict(
                f"Suite {version!r} already exists; suite versions are immutable. Use a new version."
            )
        self._record("suite.created", project_id, version, content_hash=digest, tasks=str(len(tasks)))
        return digest

    def get_suite(self, project_id: str, version: str) -> list[Task]:
        """The tasks including hidden tests, so author or higher; viewers can list versions only."""
        self._resolve(project_id, Action.SUBMIT)
        found = self._b.get_suite(self._p.tenant_id, project_id, version)
        if found is None:
            raise NotFound(f"Suite {version!r} was not found in project {project_id!r}.")
        return found

    def list_suites(self, project_id: str) -> list[str]:
        self._resolve(project_id, Action.READ)
        return self._b.list_suites(self._p.tenant_id, project_id)

    # -- judge calibration ------------------------------------------------------------------------------------------

    def put_calibration(self, project_id: str, record: CalibrationRecord) -> None:
        """Record calibration evidence (approver or higher: the eval owner vouches for it). Immutable and audited."""
        self._resolve(project_id, Action.APPROVE)
        if not self._b.put_calibration(self._p.tenant_id, project_id, record):
            raise Conflict(
                f"Calibration {record.calibration_id!r} already exists; calibration records are immutable."
            )
        self._record(
            "judge.calibrated", project_id, record.judge_version, calibration_id=record.calibration_id,
            passed=str(record.passed), items=str(record.items), set_hash=record.set_hash,
        )  # fmt: skip

    def list_calibrations(self, project_id: str) -> list[CalibrationRecord]:
        self._resolve(project_id, Action.READ)
        return self._b.list_calibrations(self._p.tenant_id, project_id)

    def calibrated_judges(self, project_id: str, now: datetime | None = None) -> frozenset[str]:
        """Judge versions whose newest calibration passed and is still current."""
        self._resolve(project_id, Action.READ)
        return frozenset(
            passing_versions(
                self._b.list_calibrations(self._p.tenant_id, project_id), now or datetime.now(UTC)
            )
        )

    # -- human labeling ---------------------------------------------------------------------------------------------

    def _batch(self, project_id: str, batch_id: str) -> labeling.LabelBatch:
        found = self._b.get_label_batch(self._p.tenant_id, project_id, batch_id)
        if found is None:
            raise NotFound(f"Labeling batch {batch_id!r} was not found in project {project_id!r}.")
        return found

    def create_label_batch(
        self, project_id: str, guideline: str, items: list[labeling.LabelItemIn], double_share: float
    ) -> labeling.LabelBatch:
        """Approver or higher: the eval owner picks what is labeled and under which guideline. Items are blind by schema."""
        self._resolve(project_id, Action.APPROVE)
        batch = labeling.LabelBatch(
            batch_id=uuid.uuid4().hex, guideline=guideline, items=labeling.make_items(items, double_share),
            created_by=self._p.subject, created_at=datetime.now(UTC),
        )  # fmt: skip
        self._b.put_label_batch(self._p.tenant_id, project_id, batch)
        self._record(
            "labeling.batch_created", project_id, batch.batch_id, items=str(len(items)),
            double_reviewed=str(sum(i.reviews_needed == 2 for i in batch.items)),
        )  # fmt: skip
        return batch

    def next_label_item(self, project_id: str, batch_id: str) -> tuple[str, labeling.LabelItem | None]:
        """The guideline and the caller's next item to score, if any. Author or higher."""
        self._resolve(project_id, Action.SUBMIT)
        batch = self._batch(project_id, batch_id)
        labels = self._b.list_labels(self._p.tenant_id, project_id, batch_id)
        return batch.guideline, labeling.next_item(batch, labels, self._p.subject)

    def put_label(self, project_id: str, batch_id: str, item_id: int, score: int) -> None:
        self._resolve(project_id, Action.SUBMIT)
        batch = self._batch(project_id, batch_id)
        item = next((i for i in batch.items if i.item_id == item_id), None)
        if item is None:
            raise NotFound(f"Item {item_id} is not in batch {batch_id!r}.")
        labels = self._b.list_labels(self._p.tenant_id, project_id, batch_id)
        if any(x.item_id == item_id and x.reviewer == self._p.subject for x in labels):
            raise Conflict(f"You already scored item {item_id}; labels are immutable.")
        if labeling.state(item, labels) != "open":
            raise Conflict(f"Item {item_id} already has all the reviews it needs. Ask for your next item.")
        label = labeling.Label(
            batch_id=batch_id, item_id=item_id, reviewer=self._p.subject, score=score, created_at=datetime.now(UTC)
        )  # fmt: skip
        if not self._b.put_label(self._p.tenant_id, project_id, label):
            raise Conflict(f"You already scored item {item_id}; labels are immutable.")
        self._record(
            "labeling.reviewed", project_id, batch_id, item_id=str(item_id)
        )  # never the score: review is blind

    def adjudicate(self, project_id: str, batch_id: str, item_id: int, score: int) -> None:
        """Approver or higher settles a disagreement; the two reviewers cannot settle their own."""
        self._resolve(project_id, Action.APPROVE)
        batch = self._batch(project_id, batch_id)
        item = next((i for i in batch.items if i.item_id == item_id), None)
        if item is None:
            raise NotFound(f"Item {item_id} is not in batch {batch_id!r}.")
        labels = self._b.list_labels(self._p.tenant_id, project_id, batch_id)
        if labeling.state(item, labels) != "disagreement":
            raise Conflict(f"Item {item_id} has no open disagreement to adjudicate.")
        if any(x.item_id == item_id and x.reviewer == self._p.subject for x in labels):
            raise PermissionDenied("You scored this item yourself, so another approver must adjudicate it.")
        label = labeling.Label(
            batch_id=batch_id, item_id=item_id, reviewer=self._p.subject, score=score, kind="adjudication",
            created_at=datetime.now(UTC),
        )  # fmt: skip
        self._b.put_label(self._p.tenant_id, project_id, label)
        self._record("labeling.adjudicated", project_id, batch_id, item_id=str(item_id))

    def label_status(self, project_id: str, batch_id: str) -> labeling.Status:
        self._resolve(project_id, Action.READ)
        batch = self._batch(project_id, batch_id)
        return labeling.status(batch, self._b.list_labels(self._p.tenant_id, project_id, batch_id))

    def export_labels(self, project_id: str, batch_id: str) -> list[Labeled]:
        """Golden labels for ``evalry judge-calibrate`` (approver or higher: they contain the code)."""
        self._resolve(project_id, Action.APPROVE)
        batch = self._batch(project_id, batch_id)
        out = labeling.export(batch, self._b.list_labels(self._p.tenant_id, project_id, batch_id))
        self._record("labeling.exported", project_id, batch_id, items=str(len(out)))
        return out

    # -- disputes over gate failures ---------------------------------------------------------------------------------

    def open_dispute(
        self, project_id: str, decision_id: str, reason: str, task_ids: list[str]
    ) -> disputes.Dispute:
        """A team member contests a blocking decision. One dispute per decision; the decision itself is never changed."""
        self._resolve(project_id, Action.SUBMIT)
        decision = self._b.get_decision(self._p.tenant_id, project_id, decision_id)
        if decision is None:
            raise NotFound(f"Decision {decision_id!r} was not found in project {project_id!r}.")
        if decision.outcome != "block":
            raise Conflict(f"Decision {decision_id!r} passed; only a blocking decision can be disputed.")
        run = self._b.get_run(self._p.tenant_id, project_id, decision.run_id)
        known = {r.task_id for r in run.results} if run else set()
        if unknown := sorted(set(task_ids) - known):
            raise ValueError(
                f"Tasks {unknown} are not in run {decision.run_id!r}. Contest tasks the run actually graded."
            )
        dispute = disputes.Dispute(
            decision_id=decision_id, run_id=decision.run_id, opened_by=self._p.subject, reason=reason,
            task_ids=task_ids, created_at=datetime.now(UTC),
        )  # fmt: skip
        if not self._b.put_dispute(self._p.tenant_id, project_id, dispute):
            raise Conflict(
                f"Decision {decision_id!r} already has a dispute. Re-gate the run to get a new decision."
            )
        self._record(
            "dispute.opened", project_id, decision_id, run_id=decision.run_id, tasks=",".join(task_ids)
        )
        return dispute

    def get_dispute(self, project_id: str, decision_id: str) -> disputes.DisputeView:
        self._resolve(project_id, Action.READ)
        dispute = self._b.get_dispute(self._p.tenant_id, project_id, decision_id)
        if dispute is None:
            raise NotFound(f"Decision {decision_id!r} has no dispute in project {project_id!r}.")
        resolution = self._b.get_resolution(self._p.tenant_id, project_id, decision_id)
        return disputes.DisputeView(
            dispute=dispute, resolution=resolution, state="resolved" if resolution else "open"
        )

    def list_disputes(self, project_id: str) -> list[disputes.DisputeView]:
        self._resolve(project_id, Action.READ)
        return [
            self.get_dispute(project_id, d.decision_id)
            for d in self._b.list_disputes(self._p.tenant_id, project_id)
        ]

    def resolve_dispute(
        self, project_id: str, decision_id: str, body: disputes.ResolutionIn
    ) -> disputes.Resolution:
        """Approver or higher, and not the person who opened it. The resolver is the named approver of any exception."""
        self._resolve(project_id, Action.APPROVE)
        dispute = self._b.get_dispute(self._p.tenant_id, project_id, decision_id)
        if dispute is None:
            raise NotFound(f"Decision {decision_id!r} has no dispute in project {project_id!r}.")
        try:
            resolution = disputes.resolve(dispute, body, resolver=self._p.subject, now=datetime.now(UTC))
        except PermissionError as e:
            self._deny(project_id, Action.APPROVE, "resolver opened the dispute")
            raise PermissionDenied(str(e)) from e
        if resolution.outcome == "task_corrected" and (
            self._b.get_suite(self._p.tenant_id, project_id, resolution.correction_ref or "") is None
        ):
            raise ValueError(
                f"Suite {resolution.correction_ref!r} does not exist in this project. Create the corrected suite first."
            )
        if not self._b.put_resolution(self._p.tenant_id, project_id, resolution):
            raise Conflict(
                f"The dispute on decision {decision_id!r} is already resolved; resolutions are immutable."
            )
        self._record(
            "dispute.resolved", project_id, decision_id, outcome=resolution.outcome,
            correction_ref=resolution.correction_ref or "", exceptions=str(len(resolution.exceptions)),
        )  # fmt: skip
        return resolution

    def dispute_exceptions(self, project_id: str, decision_id: str, run_id: str) -> list[GateException]:
        """Exceptions an approver granted when resolving this dispute, for re-gating the same run."""
        view = self.get_dispute(project_id, decision_id)
        if view.resolution is None or view.resolution.outcome != "exception_approved":
            raise Conflict(f"The dispute on decision {decision_id!r} has no approved exception to apply.")
        if view.dispute.run_id != run_id:
            raise Conflict(f"That dispute concerns run {view.dispute.run_id!r}, not {run_id!r}.")
        return view.resolution.exceptions

    # -- eval health --------------------------------------------------------------------------------------------------

    def eval_health(self, project_id: str, now: datetime | None = None) -> health.EvalHealth:
        """Derived from stored records, so any viewer may read it. Reads runs without fetching code blobs (digests suffice).

        shortcut: loads every run of the project; cap or pre-aggregate before a project has thousands.
        """
        self._resolve(project_id, Action.READ)
        t = self._p.tenant_id
        runs = [r for rid in self._b.list_runs(t, project_id) if (r := self._b.get_run(t, project_id, rid))]
        latest = max(runs, key=lambda r: r.started_at, default=None)
        suite = self._b.get_suite(t, project_id, latest.suite_version) if latest else None
        pairs = [
            (d, self._b.get_resolution(t, project_id, d.decision_id))
            for d in self._b.list_disputes(t, project_id)
        ]
        now = now or datetime.now(UTC)
        return health.compute(
            runs, {latest.suite_version: suite} if latest and suite else {}, self._b.list_calibrations(t, project_id),
            self._b.list_decisions(t, project_id), pairs, now, monitor=self.production_monitor(project_id, 30, now),
            open_incidents=len(self.open_incident_tasks(project_id)),
        )  # fmt: skip

    # -- production monitoring loop ----------------------------------------------------------------------------------

    def ingest_signals(self, project_id: str, signals: list[monitoring.Signal]) -> dict[str, int]:
        """Masked production outcomes pushed by a producer holding the author role. Idempotent by signal id."""
        self._resolve(project_id, Action.SUBMIT)
        new = self._b.put_signals(self._p.tenant_id, project_id, signals)
        self._record(
            "production.signals_ingested", project_id, project_id, received=str(len(signals)), new=str(new)
        )
        return {"received": len(signals), "new": new}

    def _offline_scores(self, project_id: str) -> tuple[str | None, dict[str, float]]:
        t = self._p.tenant_id
        runs = [r for rid in self._b.list_runs(t, project_id) if (r := self._b.get_run(t, project_id, rid))]
        latest = max(runs, key=lambda r: r.started_at, default=None)
        if latest is None:
            return None, {}
        by_task: dict[str, list[float]] = {}
        for s in latest.results:
            by_task.setdefault(s.task_id, []).append(s.composite)
        return latest.run_id, {k: sum(v) / len(v) for k, v in by_task.items()}

    def open_incident_tasks(self, project_id: str) -> set[str]:
        t = self._p.tenant_id
        closed = {c.incident_id for c in self._b.list_closures(t, project_id)}
        return {i.task_id for i in self._b.list_incidents(t, project_id) if i.incident_id not in closed}

    def production_monitor(
        self, project_id: str, days: int = 30, now: datetime | None = None
    ) -> monitoring.MonitorReport:
        self._resolve(project_id, Action.READ)
        now = now or datetime.now(UTC)
        run_id, offline = self._offline_scores(project_id)
        signals = self._b.list_signals(self._p.tenant_id, project_id, now - timedelta(days=days))
        return monitoring.compare(
            offline, signals, run_id=run_id, window_days=days, open_incident_tasks=self.open_incident_tasks(project_id), now=now
        )  # fmt: skip

    def open_incident(self, project_id: str, task_id: str, kind: str, note: str) -> monitoring.Incident:
        """Approver or higher records an eval incident, typically from a monitor finding. One open incident per task."""
        self._resolve(project_id, Action.APPROVE)
        if task_id in self.open_incident_tasks(project_id):
            raise Conflict(f"Task {task_id!r} already has an open incident. Close it before opening another.")
        incident = monitoring.Incident(
            incident_id=uuid.uuid4().hex, task_id=task_id, kind=kind,  # type: ignore[arg-type]
            note=note, opened_by=self._p.subject, created_at=datetime.now(UTC),
        )  # fmt: skip
        self._b.put_incident(self._p.tenant_id, project_id, incident)
        self._record("incident.opened", project_id, incident.incident_id, task_id=task_id, kind=kind)
        return incident

    def list_incidents(self, project_id: str) -> list[tuple[monitoring.Incident, monitoring.Closure | None]]:
        self._resolve(project_id, Action.READ)
        t = self._p.tenant_id
        closed = {c.incident_id: c for c in self._b.list_closures(t, project_id)}
        return [(i, closed.get(i.incident_id)) for i in self._b.list_incidents(t, project_id)]

    def close_incident(
        self, project_id: str, incident_id: str, body: monitoring.ClosureIn
    ) -> monitoring.Closure:
        """Close by adding tasks that reproduce the failure (they must exist in the named suite), or by saying why it is not
        an eval problem. Approver or higher."""
        self._resolve(project_id, Action.APPROVE)
        t = self._p.tenant_id
        if not any(i.incident_id == incident_id for i in self._b.list_incidents(t, project_id)):
            raise NotFound(f"Incident {incident_id!r} was not found in project {project_id!r}.")
        if body.outcome == "tasks_added":
            if not body.suite_version or not body.added_task_ids:
                raise ValueError(
                    "tasks_added needs suite_version and the added_task_ids that reproduce the failure."
                )
            suite = self._b.get_suite(t, project_id, body.suite_version)
            if suite is None:
                raise ValueError(
                    f"Suite {body.suite_version!r} does not exist in this project. Create it first."
                )
            if missing := sorted(set(body.added_task_ids) - {x.id for x in suite}):
                raise ValueError(f"Tasks {missing} are not in suite {body.suite_version!r}.")
        elif body.suite_version or body.added_task_ids:
            raise ValueError("not_an_eval_problem cannot carry added tasks.")
        closure = monitoring.Closure(
            incident_id=incident_id, outcome=body.outcome, rationale=body.rationale, suite_version=body.suite_version,
            added_task_ids=body.added_task_ids, closed_by=self._p.subject, created_at=datetime.now(UTC),
        )  # fmt: skip
        if not self._b.put_closure(t, project_id, closure):
            raise Conflict(f"Incident {incident_id!r} is already closed; closures are immutable.")
        self._record(
            "incident.closed",
            project_id,
            incident_id,
            outcome=body.outcome,
            added=str(len(body.added_task_ids)),
        )
        return closure
