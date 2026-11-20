"""Disputes over gate failures (plan §20): a team contests a blocking decision, and an approver settles it.

Decisions stay immutable. A dispute is one record per blocking decision plus one resolution, both append-only. The outcome is
either an approved exception (a real ``GateException`` with a named approver and an expiry, applied by re-gating the same run)
or a correction of the task, judge or dataset (which must point at what changed, and is then proven by a new run), or a
rejection with reasons. Every step is audited.
"""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evalry.gates.hard import GateException

MAX_EXCEPTION = timedelta(days=30)  # this module's choice: a waiver is a stopgap, so it cannot be open-ended

Outcome = Literal["rejected", "exception_approved", "task_corrected", "judge_corrected", "dataset_corrected"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Dispute(_Strict):
    decision_id: str
    run_id: str
    opened_by: str
    reason: str = Field(min_length=1, max_length=10_000)
    task_ids: list[str] = Field(min_length=1, description="The tasks the team says were wrongly failed.")
    created_at: datetime


class ExceptionIn(_Strict):
    task_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expires_at: datetime


class ResolutionIn(_Strict):
    outcome: Outcome
    rationale: str = Field(min_length=1, max_length=10_000)
    exceptions: list[ExceptionIn] = []
    correction_ref: str | None = Field(
        default=None, description="What was corrected: a new suite version, judge version or dataset version."
    )


class Resolution(_Strict):
    decision_id: str
    resolved_by: str
    outcome: Outcome
    rationale: str
    exceptions: list[GateException] = []
    correction_ref: str | None = None
    created_at: datetime


class DisputeView(_Strict):
    dispute: Dispute
    resolution: Resolution | None
    state: Literal["open", "resolved"]


def resolve(dispute: Dispute, body: ResolutionIn, *, resolver: str, now: datetime) -> Resolution:
    """Validate a resolution against its dispute and build it. The resolver becomes the named approver of any exception."""
    if resolver == dispute.opened_by:
        raise PermissionError("You opened this dispute, so another approver must resolve it.")
    exceptions: list[GateException] = []
    if body.outcome == "exception_approved":
        if not body.exceptions:
            raise ValueError(
                "Approving an exception needs at least one task exception with a reason and an expiry."
            )
        for e in body.exceptions:
            if e.task_id not in dispute.task_ids:
                raise ValueError(
                    f"Task {e.task_id!r} was not contested in this dispute (contested: {dispute.task_ids})."
                )
            if not now < e.expires_at <= now + MAX_EXCEPTION:
                raise ValueError(
                    f"An exception must expire within {MAX_EXCEPTION.days} days from now (task {e.task_id!r})."
                )
            exceptions.append(
                GateException(task_id=e.task_id, approver=resolver, reason=e.reason, expires_at=e.expires_at)
            )
    elif body.exceptions:
        raise ValueError("Only an approved exception outcome can carry exceptions.")
    if body.outcome.endswith("_corrected") and not body.correction_ref:
        raise ValueError(
            f"{body.outcome} must name what changed in correction_ref (a new suite, judge or dataset version)."
        )
    if not body.outcome.endswith("_corrected") and body.correction_ref:
        raise ValueError("correction_ref only applies to a corrected outcome.")
    return Resolution(
        decision_id=dispute.decision_id, resolved_by=resolver, outcome=body.outcome, rationale=body.rationale,
        exceptions=exceptions, correction_ref=body.correction_ref, created_at=now,
    )  # fmt: skip
