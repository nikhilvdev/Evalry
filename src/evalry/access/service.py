"""Request-level operations: what an API handler (or any caller acting for a user) is allowed to do.

Each function checks the caller's role before doing any work, runs the engine under the caller's tenant and project, and
stores results through ``ScopedStore`` so isolation and audit apply. The local CLI stays unscoped on purpose (plan §18: local
mode needs no platform); it does not go through here.
"""

from collections.abc import Callable
from datetime import datetime

from evalry.access.store import Action, PermissionDenied, Role, ScopedStore
from evalry.audit.chain import AuditLog
from evalry.gates.hard import Environment, GateDecision, GateException, HardGateRule, decide
from evalry.generation.base import Generator
from evalry.scheduler.suite import run_suite
from evalry.schemas import RunRecord, Task


def submit_suite(
    scope: ScopedStore,
    project_id: str,
    tasks: list[Task],
    make_generator: Callable[[Task, int], Generator],
    *,
    samples: int,
    suite_version: str,
    audit: AuditLog,
    budget_usd: float = float("inf"),
    workers: int = 1,
) -> RunRecord:
    """Run a suite for an author and store the immutable record. Refused before any container starts if the role is too low."""
    scope.require(project_id, Action.SUBMIT)
    record = run_suite(
        tasks, make_generator, samples=samples, suite_version=suite_version, budget_usd=budget_usd, workers=workers,
        project_id=project_id, tenant_id=scope.tenant_id, audit=audit, actor=scope.subject,
    )  # fmt: skip
    scope.put_run(project_id, record)
    return record


def gate_run(
    scope: ScopedStore,
    project_id: str,
    run_id: str,
    rule: HardGateRule,
    *,
    environment: Environment,
    key: bytes | None,
    audit: AuditLog,
    exceptions: list[GateException] | None = None,
    now: datetime | None = None,
) -> GateDecision:
    """Gate a stored run. Needs the approver role, and every exception's named approver must hold it too."""
    scope.require(project_id, Action.APPROVE)
    record = scope.get_run(project_id, run_id)
    for e in exceptions or []:
        if not scope.holds_at_least(project_id, e.approver, Role.APPROVER):
            audit.append(
                scope.subject, "gate.exception.rejected", f"{project_id}/{rule.name}/{e.task_id}", source="service",
                tenant_id=scope.tenant_id, project_id=project_id, details={"named_approver": e.approver},
            )  # fmt: skip
            raise PermissionDenied(
                f"{e.approver!r} does not hold the approver role on project {project_id!r}, so they cannot approve "
                f"an exception for task {e.task_id!r}. Name an approver or ask a project admin to grant the role."
            )
    calibrated = scope.calibrated_judges(project_id, now)
    if rule.grader == "judge" and rule.grader_version not in calibrated:
        raise PermissionDenied(
            f"Judge version {rule.grader_version!r} has no current passing calibration in project {project_id!r}, so it "
            "cannot gate. Record a passing calibration (POST /v1/projects/{project}/judge-calibrations) or gate on a "
            "deterministic grader."
        )
    decision = decide(
        record, rule, actor=scope.subject, environment=environment, project_id=project_id, tenant_id=scope.tenant_id,
        key=key, audit=audit, exceptions=exceptions, now=now, calibrated_judges=calibrated,
    )  # fmt: skip
    scope.put_decision(project_id, decision)
    return decision
