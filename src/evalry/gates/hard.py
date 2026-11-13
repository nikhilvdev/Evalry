"""Hard gate (plan §6, §17): block a release when deterministic correctness falls below a floor.

Only deterministic graders gate in phase 2. A decision is immutable, signed, and written to the audit log; only the
production environment records it as official, every other environment records a rehearsal. shortcut: HMAC with a shared
key stands in for asymmetric signatures; key custody and an asymmetric scheme are a security-team decision.
"""

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evalry.audit.chain import AuditLog
from evalry.schemas import RunRecord

Environment = Literal["dev", "staging", "production"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HardGateRule(_Frozen):
    """What a run must satisfy. Names an approved suite version, never "latest"."""

    name: str = Field(min_length=1)
    suite_version: str = Field(
        min_length=1, description="The approved, immutable suite version this gate applies to."
    )
    grader: str = "tests"
    grader_version: str = Field(
        min_length=1, description="Results from any other grader version do not count."
    )
    floor: float = Field(
        default=1.0, ge=0, le=1, description="A sample passes when its grader score is at least this."
    )
    min_pass_rate: float = Field(default=1.0, ge=0, le=1)
    required_tasks: list[str] = []


class GateException(_Frozen):
    """Waives one task's failures. Needs a named approver and an expiry, and is logged when applied."""

    task_id: str = Field(min_length=1)
    approver: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    expires_at: datetime


class GateDecision(_Frozen):
    decision_id: str
    gate: str
    rule_hash: str
    run_id: str
    suite_version: str
    environment: Environment
    outcome: Literal["pass", "block"]
    reasons: list[str]
    waived: list[GateException]
    pass_rate: float | None
    samples: int
    actor: str
    decided_at: datetime
    signature: str = ""


def _canonical(decision: GateDecision) -> bytes:
    body = decision.model_dump(mode="json", exclude={"signature"})
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(decision: GateDecision, key: bytes) -> GateDecision:
    return decision.model_copy(
        update={"signature": hmac.new(key, _canonical(decision), hashlib.sha256).hexdigest()}
    )


def verify(decision: GateDecision, key: bytes) -> bool:
    expected = hmac.new(key, _canonical(decision), hashlib.sha256).hexdigest()
    return hmac.compare_digest(decision.signature, expected)


def decide(
    record: RunRecord,
    rule: HardGateRule,
    *,
    actor: str,
    environment: Environment,
    project_id: str,
    key: bytes | None,
    tenant_id: str | None = None,
    audit: AuditLog,
    exceptions: list[GateException] | None = None,
    now: datetime | None = None,
) -> GateDecision:
    """Evaluate the run against the rule, sign the decision, and record it in the audit log."""
    if environment == "production" and not key:
        raise ValueError("Production gate decisions must be signed. Set a signing key (EVALRY_GATE_KEY).")
    now = now or datetime.now(UTC)
    reasons: list[str] = []
    waived: list[GateException] = []
    pass_rate: float | None = None

    if record.suite_version != rule.suite_version:
        reasons.append(
            f"run is for suite {record.suite_version!r}, the gate requires approved suite {rule.suite_version!r}"
        )
    elif not record.results:
        reasons.append("run has no results")
    else:
        weak = sorted({r.isolation or "unrecorded" for r in record.results} - {"rpc"})
        if weak:
            reasons.append(f"hard gates require isolation 'rpc'; found {weak}")
        missing = sorted(set(rule.required_tasks) - {r.task_id for r in record.results})
        if missing:
            reasons.append(f"required tasks missing from the run: {missing}")

        valid = {e.task_id: e for e in (exceptions or []) if e.expires_at > now}
        passed = counted = 0
        failing_tasks: set[str] = set()
        for r in record.results:
            score = next(
                (s for s in r.scores if s.grader == rule.grader and s.grader_version == rule.grader_version),
                None,
            )
            ok = score is not None and score.score >= rule.floor
            if not ok and r.task_id in valid:
                if valid[r.task_id] not in waived:
                    waived.append(valid[r.task_id])
                continue
            counted += 1
            passed += ok
            if not ok:
                failing_tasks.add(r.task_id)
        pass_rate = passed / counted if counted else None
        if pass_rate is None:
            reasons.append("every sample was waived by exceptions; nothing was actually gated")
        elif pass_rate < rule.min_pass_rate:
            reasons.append(
                f"pass rate {pass_rate:.3f} is below {rule.min_pass_rate:.3f}; failing tasks: {sorted(failing_tasks)}"
            )

    decision = GateDecision(
        decision_id=uuid.uuid4().hex,
        gate=rule.name,
        rule_hash=hashlib.sha256(rule.model_dump_json().encode()).hexdigest(),
        run_id=record.run_id,
        suite_version=record.suite_version,
        environment=environment,
        outcome="block" if reasons else "pass",
        reasons=reasons,
        waived=waived,
        pass_rate=pass_rate,
        samples=len(record.results),
        actor=actor,
        decided_at=now,
    )
    if key:
        decision = sign(decision, key)
    action = "gate.decided" if environment == "production" else "gate.rehearsed"
    audit.append(
        actor,
        action,
        f"{project_id}/{rule.name}",
        source="gate",
        tenant_id=tenant_id,
        project_id=project_id,
        details={
            "decision_id": decision.decision_id,
            "run_id": decision.run_id,
            "outcome": decision.outcome,
            "environment": environment,
            "signed": str(bool(key)).lower(),
        },
    )
    for e in waived:
        audit.append(
            e.approver,
            "gate.exception.applied",
            f"{project_id}/{rule.name}/{e.task_id}",
            source="gate",
            tenant_id=tenant_id,
            project_id=project_id,
            details={
                "decision_id": decision.decision_id,
                "expires_at": e.expires_at.isoformat(),
                "reason": e.reason[:200],
            },
        )
    return decision
