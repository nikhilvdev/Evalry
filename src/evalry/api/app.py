"""FastAPI application factory. State and configuration are injected so tests, dev and production share one code path.

Environment and the gate signing key are server configuration, never request fields: a caller cannot ask for a
"production" decision or supply their own key. shortcut: the request body limit only checks Content-Length, so chunked
uploads bypass it; put a proxy limit in front in deployment.
"""

import hashlib
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from logquill import bind_context
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field

from evalry import disputes, health, labeling, monitoring
from evalry.access.service import gate_run
from evalry.access.store import (
    AccessError,
    Action,
    Conflict,
    NotFound,
    PermissionDenied,
    Principal,
    Role,
    ScopedStore,
    Store,
)
from evalry.api.auth import AuthError, TokenVerifier
from evalry.audit.chain import AuditLog
from evalry.gates.hard import Environment, GateDecision, GateException, HardGateRule
from evalry.judge.calibration import MIN_ITEMS, CalibrationRecord, CalibrationReport, Labeled, meets_targets
from evalry.log import get_logger
from evalry.scheduler.jobs import Job, JobQueue, JobStore, Priority, State
from evalry.scheduler.worker import collect, submit_run
from evalry.schemas import RunRecord, Task

log = get_logger("api")

_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
ProjectId = Annotated[str, Path(pattern=_PROJECT_ID.pattern)]
SubjectId = Annotated[str, Path(min_length=1, max_length=256)]
_MAX_BODY = 5 * 1024 * 1024


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateProject(_Body):
    project_id: str


class GrantRole(_Body):
    role: Literal["viewer", "author", "approver", "admin"]


class GateRequest(_Body):
    rule: HardGateRule
    exceptions: list[GateException] = []
    dispute_decision_id: str | None = Field(
        default=None,
        description="Apply the exceptions an approver granted when resolving the dispute on this decision.",
    )


class SuiteBody(_Body):
    tasks: list[Task]


class SuiteVersions(_Body):
    versions: list[str]


class RunRequest(_Body):
    suite_version: str
    samples: int = Field(default=1, ge=1, le=100)
    priority: Literal["smoke", "nightly"] = "nightly"
    generator: str = "default"  # names the worker's generator configuration; part of every job key


class RunStatus(_Body):
    run_id: str
    state: Literal["running", "failed", "done"]
    jobs: dict[str, int]
    error: str = ""
    recorded: bool = False


class LabelBatchRequest(_Body):
    guideline: str = Field(min_length=1, max_length=50_000)
    items: list[labeling.LabelItemIn] = Field(min_length=1, max_length=1000)
    double_share: float = Field(default=0.2, ge=0, le=1)


class NextLabelItem(_Body):
    guideline: str
    item: labeling.LabelItem | None


class Score(_Body):
    score: int = Field(ge=1, le=5)


class DisputeRequest(_Body):
    reason: str = Field(min_length=1, max_length=10_000)
    task_ids: list[str] = Field(min_length=1)


class SignalBatch(_Body):
    signals: list[monitoring.Signal] = Field(min_length=1, max_length=1000)


class IncidentRequest(_Body):
    task_id: str = Field(min_length=1)
    kind: Literal["offline_pass_production_fail", "offline_fail_production_pass", "other"] = "other"
    note: str = Field(min_length=1, max_length=10_000)


class IncidentView(_Body):
    incident: monitoring.Incident
    closure: monitoring.Closure | None
    state: Literal["open", "closed"]


class RunIds(_Body):
    run_ids: list[str]


def create_app(
    *,
    store: Store,
    audit: AuditLog,
    verifier: TokenVerifier,
    environment: Environment,
    gate_key: bytes | None,
    queue_for: Callable[[str], JobStore] | None = None,
) -> FastAPI:
    """``queue_for(tenant_id)`` returns the job queue bound to that tenant. Default: one in-process queue, so a worker must
    share this process (tests); a real deployment passes the PostgreSQL queue and runs ``evalry worker`` separately."""
    if queue_for is None:
        local = JobQueue()
        queue_for = lambda tenant: local
    if environment == "production" and not gate_key:
        raise ValueError("A production API must have a gate signing key. Set EVALRY_GATE_KEY.")
    app = FastAPI(title="Evalry", version="1.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        if int(request.headers.get("content-length") or 0) > _MAX_BODY:
            return JSONResponse({"error": f"Request body is over {_MAX_BODY} bytes."}, status_code=413)
        start = time.perf_counter()
        with bind_context(request_id=request_id):
            response = await call_next(request)
            principal: Principal | None = getattr(request.state, "principal", None)
            route = request.scope.get("route")
            log.info(
                "request",
                method=request.method,
                route=getattr(route, "path", "unmatched"),  # the template, never the raw path with ids
                status=response.status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
                tenant_id=principal.tenant_id if principal else None,
                actor=principal.subject if principal else None,
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AuthError)
    async def _auth(request: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=401, headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(psycopg.OperationalError)
    @app.exception_handler(PoolTimeout)
    async def _db_down(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "database unavailable", error=type(exc).__name__
        )  # the message can hold the DSN; log the type only
        return JSONResponse(
            {"error": "The database is unavailable. Try again shortly."},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(AccessError)
    async def _access(request: Request, exc: AccessError) -> JSONResponse:
        code = {NotFound: 404, PermissionDenied: 403, Conflict: 409}.get(type(exc), 400)
        return JSONResponse({"error": str(exc)}, status_code=code)

    def scoped(request: Request, authorization: Annotated[str | None, Header()] = None) -> ScopedStore:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("The access token is missing, invalid or expired. Sign in again.")
        try:
            principal = verifier.verify(token.strip())
        except AuthError:
            log.warn("token rejected")  # noqa: G010 (LogQuill API); the token itself is never logged
            raise
        request.state.principal = principal
        return store.view(principal)

    Scope = Annotated[ScopedStore, Depends(scoped)]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/projects", status_code=201)
    def create_project(body: CreateProject, scope: Scope) -> dict[str, str]:
        if not _valid_project(body.project_id):
            raise AccessError(
                "project_id must be lowercase letters, digits and hyphens, up to 63 characters."
            )
        scope.create_project(body.project_id)
        return {"project_id": body.project_id}

    @app.put("/v1/projects/{project_id}/members/{subject}", status_code=204)
    def grant(project_id: ProjectId, subject: SubjectId, body: GrantRole, scope: Scope) -> Response:
        scope.grant(project_id, subject, Role[body.role.upper()])
        return Response(status_code=204)

    @app.delete("/v1/projects/{project_id}/members/{subject}", status_code=204)
    def revoke(project_id: ProjectId, subject: SubjectId, scope: Scope) -> Response:
        scope.revoke(project_id, subject)
        return Response(status_code=204)

    @app.post("/v1/projects/{project_id}/runs", status_code=201)
    def import_run(project_id: ProjectId, record: RunRecord, scope: Scope) -> dict[str, str]:
        """Store a run record produced elsewhere (for example a local run). Imported runs are history, not baselines."""
        scope.put_run(project_id, record)
        return {"run_id": record.run_id}

    @app.get("/v1/projects/{project_id}/runs")
    def list_runs(project_id: ProjectId, scope: Scope) -> RunIds:
        return RunIds(run_ids=scope.list_runs(project_id))

    @app.get("/v1/projects/{project_id}/runs/{run_id}")
    def get_run(project_id: ProjectId, run_id: SubjectId, scope: Scope) -> RunRecord:
        return scope.get_run(project_id, run_id)

    @app.put("/v1/projects/{project_id}/suites/{version}", status_code=201)
    def create_suite(
        project_id: ProjectId, version: SubjectId, body: SuiteBody, scope: Scope
    ) -> dict[str, str]:
        """Create an immutable suite version. Hidden tests are in the body, so this and reads need author or higher."""
        return {"suite_version": version, "content_hash": scope.put_suite(project_id, version, body.tasks)}

    @app.get("/v1/projects/{project_id}/suites")
    def list_suites(project_id: ProjectId, scope: Scope) -> SuiteVersions:
        return SuiteVersions(versions=scope.list_suites(project_id))

    @app.get("/v1/projects/{project_id}/suites/{version}")
    def get_suite(project_id: ProjectId, version: SubjectId, scope: Scope) -> SuiteBody:
        return SuiteBody(tasks=scope.get_suite(project_id, version))

    @app.post("/v1/projects/{project_id}/run-requests", status_code=202)
    def request_run(project_id: ProjectId, body: RunRequest, scope: Scope) -> dict[str, str | int]:
        """Queue a run of a stored suite version for a worker. Same suite and generator name: same run id, nothing duplicated."""
        scope.require(project_id, Action.SUBMIT)
        tasks = scope.get_suite(project_id, body.suite_version)
        generator_hash = hashlib.sha256(body.generator.encode()).hexdigest()
        run_id = hashlib.sha256(
            f"{scope.tenant_id}/{project_id}/{body.suite_version}/{generator_hash}".encode()
        ).hexdigest()[:32]
        if run_id in scope.list_runs(project_id):
            raise Conflict(
                f"Run {run_id!r} for suite {body.suite_version!r} and generator {body.generator!r} is already recorded and "
                "immutable. Use a different generator name to run it again."
            )
        jobs = submit_run(
            queue_for(scope.tenant_id), tasks, project_id=project_id, suite_version=body.suite_version,
            generator_hash=generator_hash, samples=body.samples, priority=Priority[body.priority.upper()],
            audit=audit, actor=scope.subject, tenant_id=scope.tenant_id, run_id=run_id,
        )  # fmt: skip
        return {"run_id": run_id, "jobs": len(jobs)}

    @app.get("/v1/projects/{project_id}/run-requests/{run_id}")
    def run_status(project_id: ProjectId, run_id: SubjectId, scope: Scope) -> RunStatus:
        """Progress of a queued run. Once every job is done this records the immutable run (author or higher, as workers
        cannot write runs); polling again is harmless. shortcut: ``started_at`` is the time of recording, not of submission."""
        scope.require(project_id, Action.SUBMIT)
        queue = queue_for(scope.tenant_id)
        jobs = [j for j in queue.run_jobs(run_id) if j.project_id == project_id]
        if not jobs:
            raise NotFound(f"Run request {run_id!r} was not found in project {project_id!r}.")
        counts = Counter(j.state.value for j in jobs)
        if counts[State.FAILED] and not counts[State.QUEUED] and not counts[State.RUNNING]:
            first = next(j for j in jobs if j.state is State.FAILED)
            return RunStatus(
                run_id=run_id,
                state="failed",
                jobs=dict(counts),
                error=f"{first.task_id}#{first.sample}: {first.error}",
            )
        if counts[State.DONE] < len(jobs):
            return RunStatus(run_id=run_id, state="running", jobs=dict(counts))
        if run_id not in scope.list_runs(project_id):
            scope.put_run(project_id, _record(queue, jobs, run_id))
        return RunStatus(run_id=run_id, state="done", jobs=dict(counts), recorded=True)

    @app.post("/v1/projects/{project_id}/judge-calibrations", status_code=201)
    def record_calibration(project_id: ProjectId, body: CalibrationReport, scope: Scope) -> CalibrationRecord:
        """Record the result of ``evalry judge-calibrate`` (approver or higher). ``passed`` is recomputed here from the numbers
        against the plan's targets, never taken from the caller. shortcut: the numbers themselves are the approver's word."""
        if body.items < MIN_ITEMS:
            raise AccessError(
                f"A calibration needs at least {MIN_ITEMS} human-labeled solutions; this one has {body.items}."
            )
        record = CalibrationRecord(
            **body.model_dump(exclude={"passed"}),
            passed=meets_targets(body.items, body.agreement_within_one, body.stability, body.injection_resistant),
            calibration_id=uuid.uuid4().hex, created_at=datetime.now(UTC), created_by=scope.subject,
        )  # fmt: skip
        scope.put_calibration(project_id, record)
        return record

    @app.get("/v1/projects/{project_id}/judge-calibrations")
    def list_calibrations(project_id: ProjectId, scope: Scope) -> list[CalibrationRecord]:
        return scope.list_calibrations(project_id)

    @app.post("/v1/projects/{project_id}/label-batches", status_code=201)
    def create_label_batch(
        project_id: ProjectId, body: LabelBatchRequest, scope: Scope
    ) -> dict[str, str | int]:
        """Start a blind labeling batch (approver or higher). Items carry only prompt and code."""
        batch = scope.create_label_batch(project_id, body.guideline, body.items, body.double_share)
        return {"batch_id": batch.batch_id, "items": len(batch.items)}

    @app.get("/v1/projects/{project_id}/label-batches/{batch_id}/next")
    def next_label_item(project_id: ProjectId, batch_id: SubjectId, scope: Scope) -> NextLabelItem:
        guideline, item = scope.next_label_item(project_id, batch_id)
        return NextLabelItem(guideline=guideline, item=item)

    @app.put("/v1/projects/{project_id}/label-batches/{batch_id}/items/{item_id}/label", status_code=204)
    def put_label(
        project_id: ProjectId, batch_id: SubjectId, item_id: int, body: Score, scope: Scope
    ) -> Response:
        scope.put_label(project_id, batch_id, item_id, body.score)
        return Response(status_code=204)

    @app.put(
        "/v1/projects/{project_id}/label-batches/{batch_id}/items/{item_id}/adjudication", status_code=204
    )
    def adjudicate(
        project_id: ProjectId, batch_id: SubjectId, item_id: int, body: Score, scope: Scope
    ) -> Response:
        scope.adjudicate(project_id, batch_id, item_id, body.score)
        return Response(status_code=204)

    @app.get("/v1/projects/{project_id}/label-batches/{batch_id}")
    def label_status(project_id: ProjectId, batch_id: SubjectId, scope: Scope) -> labeling.Status:
        return scope.label_status(project_id, batch_id)

    @app.get("/v1/projects/{project_id}/label-batches/{batch_id}/export")
    def export_labels(project_id: ProjectId, batch_id: SubjectId, scope: Scope) -> list[Labeled]:
        """Golden labels, ready for ``evalry judge-calibrate``. 409 until every item is final."""
        try:
            return scope.export_labels(project_id, batch_id)
        except ValueError as e:
            raise Conflict(str(e)) from e

    @app.post("/v1/projects/{project_id}/decisions/{decision_id}/dispute", status_code=201)
    def open_dispute(
        project_id: ProjectId, decision_id: SubjectId, body: DisputeRequest, scope: Scope
    ) -> disputes.Dispute:
        """Contest a blocking decision (author or higher). The decision stays as signed; the outcome is a separate record."""
        try:
            return scope.open_dispute(project_id, decision_id, body.reason, body.task_ids)
        except ValueError as e:
            raise AccessError(str(e)) from e

    @app.get("/v1/projects/{project_id}/decisions/{decision_id}/dispute")
    def get_dispute(project_id: ProjectId, decision_id: SubjectId, scope: Scope) -> disputes.DisputeView:
        return scope.get_dispute(project_id, decision_id)

    @app.get("/v1/projects/{project_id}/disputes")
    def list_disputes(project_id: ProjectId, scope: Scope) -> list[disputes.DisputeView]:
        return scope.list_disputes(project_id)

    @app.post("/v1/projects/{project_id}/decisions/{decision_id}/dispute/resolution", status_code=201)
    def resolve_dispute(
        project_id: ProjectId, decision_id: SubjectId, body: disputes.ResolutionIn, scope: Scope
    ) -> disputes.Resolution:
        """Settle a dispute (approver or higher, not its opener): reject, approve an expiring exception, or point at a correction."""
        try:
            return scope.resolve_dispute(project_id, decision_id, body)
        except ValueError as e:
            raise AccessError(str(e)) from e

    @app.get("/v1/projects/{project_id}/eval-health")
    def eval_health(project_id: ProjectId, scope: Scope) -> health.EvalHealth:
        """Can this project's scores block a release? Flakiness, saturation, coverage, judges, gates and cost, plus open issues."""
        return scope.eval_health(project_id)

    @app.post("/v1/projects/{project_id}/production-signals", status_code=202)
    def ingest_signals(project_id: ProjectId, body: SignalBatch, scope: Scope) -> dict[str, int]:
        """Masked production outcomes (never code or user content: the schema has no room for it). Resending is harmless."""
        return scope.ingest_signals(project_id, body.signals)

    @app.get("/v1/projects/{project_id}/production-monitor")
    def production_monitor(
        project_id: ProjectId, scope: Scope, days: Annotated[int, Query(ge=1, le=365)] = 30
    ) -> monitoring.MonitorReport:
        """Offline scores of the latest run against production outcomes, task by task, with findings where they disagree."""
        return scope.production_monitor(project_id, days)

    @app.post("/v1/projects/{project_id}/eval-incidents", status_code=201)
    def open_incident(project_id: ProjectId, body: IncidentRequest, scope: Scope) -> monitoring.Incident:
        return scope.open_incident(project_id, body.task_id, body.kind, body.note)

    @app.get("/v1/projects/{project_id}/eval-incidents")
    def list_incidents(project_id: ProjectId, scope: Scope) -> list[IncidentView]:
        return [
            IncidentView(incident=i, closure=c, state="closed" if c else "open")
            for i, c in scope.list_incidents(project_id)
        ]

    @app.post("/v1/projects/{project_id}/eval-incidents/{incident_id}/closure", status_code=201)
    def close_incident(
        project_id: ProjectId, incident_id: SubjectId, body: monitoring.ClosureIn, scope: Scope
    ) -> monitoring.Closure:
        try:
            return scope.close_incident(project_id, incident_id, body)
        except ValueError as e:
            raise AccessError(str(e)) from e

    @app.post("/v1/projects/{project_id}/runs/{run_id}/gate")
    def gate(project_id: ProjectId, run_id: SubjectId, body: GateRequest, scope: Scope) -> GateDecision:
        granted = (
            scope.dispute_exceptions(project_id, body.dispute_decision_id, run_id)
            if body.dispute_decision_id
            else []
        )
        return gate_run(
            scope, project_id, run_id, body.rule, environment=environment, key=gate_key, audit=audit,
            exceptions=[*body.exceptions, *granted],
        )  # fmt: skip

    @app.get("/v1/projects/{project_id}/decisions/{decision_id}")
    def get_decision(project_id: ProjectId, decision_id: SubjectId, scope: Scope) -> GateDecision:
        return scope.get_decision(project_id, decision_id)

    return app


def _record(queue: JobStore, jobs: list[Job], run_id: str) -> RunRecord:
    jobs = sorted(jobs, key=lambda j: (j.task_id, j.sample))
    return collect(
        queue, jobs, run_id=run_id, suite_version=jobs[0].suite_version, started_at=datetime.now(UTC)
    )


def _valid_project(project_id: str) -> bool:
    return _PROJECT_ID.fullmatch(project_id) is not None
