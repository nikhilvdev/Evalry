"""FastAPI application factory. State and configuration are injected so tests, dev and production share one code path.

Environment and the gate signing key are server configuration, never request fields: a caller cannot ask for a
"production" decision or supply their own key. shortcut: the request body limit only checks Content-Length, so chunked
uploads bypass it; put a proxy limit in front in deployment.
"""

import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, Path, Request, Response
from fastapi.responses import JSONResponse
from logquill import bind_context
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict

from evalry.access import (
    AccessError,
    Conflict,
    NotFound,
    PermissionDenied,
    Principal,
    Role,
    ScopedStore,
    Store,
)
from evalry.api.auth import AuthError, TokenVerifier
from evalry.audit import AuditLog
from evalry.gates import Environment, GateDecision, GateException, HardGateRule
from evalry.log import get_logger
from evalry.schemas import RunRecord
from evalry.service import gate_run

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


class RunIds(_Body):
    run_ids: list[str]


def create_app(
    *,
    store: Store,
    audit: AuditLog,
    verifier: TokenVerifier,
    environment: Environment,
    gate_key: bytes | None,
) -> FastAPI:
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

    @app.post("/v1/projects/{project_id}/runs/{run_id}/gate")
    def gate(project_id: ProjectId, run_id: SubjectId, body: GateRequest, scope: Scope) -> GateDecision:
        return gate_run(
            scope, project_id, run_id, body.rule, environment=environment, key=gate_key, audit=audit,
            exceptions=body.exceptions,
        )  # fmt: skip

    @app.get("/v1/projects/{project_id}/decisions/{decision_id}")
    def get_decision(project_id: ProjectId, decision_id: SubjectId, scope: Scope) -> GateDecision:
        return scope.get_decision(project_id, decision_id)

    return app


def _valid_project(project_id: str) -> bool:
    return _PROJECT_ID.fullmatch(project_id) is not None
