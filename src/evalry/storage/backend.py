"""Storage interface behind the access layer. Every method takes the tenant explicitly: there is no call that reads across
tenants, so a backend cannot be asked a cross-tenant question. Roles cross this boundary as small ints (viewer=1 ... admin=4).
"""

import hashlib
import json
import threading
from datetime import datetime
from typing import Protocol

from evalry.disputes import Dispute, Resolution
from evalry.gates.hard import GateDecision
from evalry.judge.calibration import CalibrationRecord
from evalry.labeling import Label, LabelBatch
from evalry.monitoring import Closure, Incident, Signal
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
    def list_decisions(self, tenant_id: str, project_id: str) -> list[GateDecision]:
        """Oldest first."""
        ...

    def put_calibration(self, tenant_id: str, project_id: str, record: CalibrationRecord) -> bool:
        """False if a record with this id already exists (calibrations are immutable)."""
        ...

    def list_calibrations(self, tenant_id: str, project_id: str) -> list[CalibrationRecord]:
        """Oldest first."""
        ...

    def put_dispute(self, tenant_id: str, project_id: str, dispute: Dispute) -> bool:
        """False if this decision already has a dispute."""
        ...

    def get_dispute(self, tenant_id: str, project_id: str, decision_id: str) -> Dispute | None: ...
    def list_disputes(self, tenant_id: str, project_id: str) -> list[Dispute]: ...
    def put_resolution(self, tenant_id: str, project_id: str, resolution: Resolution) -> bool:
        """False if the dispute is already resolved."""
        ...

    def get_resolution(self, tenant_id: str, project_id: str, decision_id: str) -> Resolution | None: ...
    def put_signals(self, tenant_id: str, project_id: str, signals: list[Signal]) -> int:
        """Insert new signals (existing ids are skipped); returns how many were new."""
        ...

    def list_signals(self, tenant_id: str, project_id: str, since: datetime) -> list[Signal]: ...
    def put_incident(self, tenant_id: str, project_id: str, incident: Incident) -> bool: ...
    def list_incidents(self, tenant_id: str, project_id: str) -> list[Incident]: ...
    def put_closure(self, tenant_id: str, project_id: str, closure: Closure) -> bool:
        """False if the incident is already closed."""
        ...

    def list_closures(self, tenant_id: str, project_id: str) -> list[Closure]: ...
    def put_label_batch(self, tenant_id: str, project_id: str, batch: LabelBatch) -> bool: ...
    def get_label_batch(self, tenant_id: str, project_id: str, batch_id: str) -> LabelBatch | None: ...
    def put_label(self, tenant_id: str, project_id: str, label: Label) -> bool:
        """False if this reviewer already recorded this kind of label for the item."""
        ...

    def list_labels(self, tenant_id: str, project_id: str, batch_id: str) -> list[Label]:
        """Oldest first."""
        ...


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
        self._calibrations: dict[tuple[str, str], list[CalibrationRecord]] = {}
        self._signals: dict[tuple[str, str, str], Signal] = {}
        self._incidents: dict[tuple[str, str, str], Incident] = {}
        self._closures: dict[tuple[str, str, str], Closure] = {}
        self._disputes: dict[tuple[str, str, str], Dispute] = {}
        self._resolutions: dict[tuple[str, str, str], Resolution] = {}
        self._batches: dict[tuple[str, str, str], LabelBatch] = {}
        self._labels: dict[tuple[str, str, str], list[Label]] = {}

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

    def put_calibration(self, tenant_id: str, project_id: str, record: CalibrationRecord) -> bool:
        with self._lock:
            records = self._calibrations.setdefault((tenant_id, project_id), [])
            if any(r.calibration_id == record.calibration_id for r in records):
                return False
            records.append(record)
            return True

    def list_calibrations(self, tenant_id: str, project_id: str) -> list[CalibrationRecord]:
        with self._lock:
            return list(self._calibrations.get((tenant_id, project_id), []))

    def put_label_batch(self, tenant_id: str, project_id: str, batch: LabelBatch) -> bool:
        with self._lock:
            key = (tenant_id, project_id, batch.batch_id)
            if key in self._batches:
                return False
            self._batches[key] = batch
            return True

    def get_label_batch(self, tenant_id: str, project_id: str, batch_id: str) -> LabelBatch | None:
        with self._lock:
            return self._batches.get((tenant_id, project_id, batch_id))

    def put_label(self, tenant_id: str, project_id: str, label: Label) -> bool:
        with self._lock:
            labels = self._labels.setdefault((tenant_id, project_id, label.batch_id), [])
            if any(
                (x.item_id, x.reviewer, x.kind) == (label.item_id, label.reviewer, label.kind) for x in labels
            ):
                return False
            labels.append(label)
            return True

    def list_labels(self, tenant_id: str, project_id: str, batch_id: str) -> list[Label]:
        with self._lock:
            return list(self._labels.get((tenant_id, project_id, batch_id), []))

    def list_decisions(self, tenant_id: str, project_id: str) -> list[GateDecision]:
        with self._lock:
            found = [d for (t, p, _), d in self._decisions.items() if (t, p) == (tenant_id, project_id)]
        return sorted(found, key=lambda d: d.decided_at)

    def put_dispute(self, tenant_id: str, project_id: str, dispute: Dispute) -> bool:
        with self._lock:
            key = (tenant_id, project_id, dispute.decision_id)
            if key in self._disputes:
                return False
            self._disputes[key] = dispute
            return True

    def get_dispute(self, tenant_id: str, project_id: str, decision_id: str) -> Dispute | None:
        with self._lock:
            return self._disputes.get((tenant_id, project_id, decision_id))

    def list_disputes(self, tenant_id: str, project_id: str) -> list[Dispute]:
        with self._lock:
            return [d for (t, p, _), d in self._disputes.items() if (t, p) == (tenant_id, project_id)]

    def put_resolution(self, tenant_id: str, project_id: str, resolution: Resolution) -> bool:
        with self._lock:
            key = (tenant_id, project_id, resolution.decision_id)
            if key in self._resolutions:
                return False
            self._resolutions[key] = resolution
            return True

    def get_resolution(self, tenant_id: str, project_id: str, decision_id: str) -> Resolution | None:
        with self._lock:
            return self._resolutions.get((tenant_id, project_id, decision_id))

    def put_signals(self, tenant_id: str, project_id: str, signals: list[Signal]) -> int:
        with self._lock:
            new = 0
            for s in signals:
                key = (tenant_id, project_id, s.signal_id)
                if key not in self._signals:
                    self._signals[key] = s
                    new += 1
            return new

    def list_signals(self, tenant_id: str, project_id: str, since: datetime) -> list[Signal]:
        with self._lock:
            return [
                s
                for (t, p, _), s in self._signals.items()
                if (t, p) == (tenant_id, project_id) and s.observed_at >= since
            ]

    def put_incident(self, tenant_id: str, project_id: str, incident: Incident) -> bool:
        with self._lock:
            key = (tenant_id, project_id, incident.incident_id)
            if key in self._incidents:
                return False
            self._incidents[key] = incident
            return True

    def list_incidents(self, tenant_id: str, project_id: str) -> list[Incident]:
        with self._lock:
            found = [i for (t, p, _), i in self._incidents.items() if (t, p) == (tenant_id, project_id)]
        return sorted(found, key=lambda i: i.created_at)

    def put_closure(self, tenant_id: str, project_id: str, closure: Closure) -> bool:
        with self._lock:
            key = (tenant_id, project_id, closure.incident_id)
            if key in self._closures:
                return False
            self._closures[key] = closure
            return True

    def list_closures(self, tenant_id: str, project_id: str) -> list[Closure]:
        with self._lock:
            return [c for (t, p, _), c in self._closures.items() if (t, p) == (tenant_id, project_id)]
