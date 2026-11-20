"""Judge service: the only process that holds the judge's LLM API key (plan §3, §6, §9).

Same contract as the generator service: shared bearer token, data classification checked before anything is sent, no prompt,
code or rationale in logs, configuration from the environment with every problem reported at once. The rubric is a file the
eval owner controls (``EVALRY_JUDGE_RUBRIC_FILE``); it is part of the judge version, so editing it makes a new judge.
"""

import hmac
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import anthropic
import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from logquill import bind_context
from pydantic import BaseModel, ConfigDict, Field

from evalry.billing.cost import Rates
from evalry.generation.service import ConfigError
from evalry.judge.base import Judge, JudgeRefused, Verdict
from evalry.judge.claude import AnthropicJudge
from evalry.log import get_logger

log = get_logger("judge-service")

_LEVELS = ("public", "internal", "confidential", "restricted")
_MAX = 200_000


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=_MAX)
    code: str = Field(max_length=_MAX)
    sample: int = Field(ge=0)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"


class _Refuse(Exception):
    def __init__(self, status: int, message: str, retry_after: str | None = None) -> None:
        self.status, self.message, self.retry_after = status, message, retry_after


def create_judge_app(judge: Judge, *, token: str, allowed: frozenset[str]) -> FastAPI:
    app = FastAPI(title="Evalry judge", version="1.0.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = uuid.uuid4().hex
        with bind_context(request_id=rid):
            response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    def authed(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
            raise _Refuse(401, "The service token is missing or wrong.")

    @app.exception_handler(_Refuse)
    async def _refused(request: Request, exc: _Refuse) -> JSONResponse:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        return JSONResponse({"error": exc.message}, status_code=exc.status, headers=headers)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/judge", dependencies=[Depends(authed)])
    def judge_endpoint(body: JudgeRequest) -> Verdict:
        if body.sensitivity not in allowed:
            raise _Refuse(
                403,
                f"Data classified {body.sensitivity!r} may not go to this provider (allowed: {sorted(allowed)}). "
                "Use an approved provider or reclassify the task.",
            )
        try:
            out = judge.judge(body.prompt, body.code, sample=body.sample)
        except JudgeRefused as e:
            raise _Refuse(422, str(e)) from e
        except anthropic.RateLimitError as e:
            raise _Refuse(
                429,
                "The provider rate-limited this request. Retry shortly.",
                e.response.headers.get("retry-after", "30"),
            ) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            log.error("provider error", error=type(e).__name__, status=getattr(e, "status_code", None))
            raise _Refuse(502, "The provider failed this request. Retry shortly.") from e
        except ValueError as e:  # malformed verdict: retryable, and never a silent score
            log.error("invalid verdict", error=type(e).__name__)
            raise _Refuse(502, str(e)) from e
        log.info(
            "judged", sample=body.sample, model=out.model, judge_version=out.judge_version, score=out.score
        )
        return out

    return app


@dataclass(frozen=True)
class Settings:
    model: str
    rubric: str
    rates: Rates
    token: str
    allowed: frozenset[str]
    host: str
    port: int


def load_settings(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []

    def get(name: str) -> str | None:
        return env.get(name) or None

    model = get("EVALRY_JUDGE_MODEL")
    if not model:
        problems.append("EVALRY_JUDGE_MODEL is required; there is no default judge model.")
    rubric = ""
    if path := get("EVALRY_JUDGE_RUBRIC_FILE"):
        try:
            rubric = Path(path).read_text().strip()
        except OSError as e:
            problems.append(f"EVALRY_JUDGE_RUBRIC_FILE cannot be read ({type(e).__name__}).")
        else:
            if not rubric:
                problems.append("EVALRY_JUDGE_RUBRIC_FILE is empty; the judge needs a rubric.")
    else:
        problems.append("EVALRY_JUDGE_RUBRIC_FILE is required: the rubric file the eval owner approved.")
    rates: list[float] = []
    for name in ("EVALRY_JUDGE_USD_PER_MTOK_IN", "EVALRY_JUDGE_USD_PER_MTOK_OUT"):
        try:
            rates.append(float(get(name) or "x"))
            if rates[-1] < 0:
                raise ValueError
        except ValueError:
            problems.append(
                f"{name} is required: the provider price in USD per million tokens (no defaults)."
            )
            rates.append(0.0)
    token = get("EVALRY_JUDGE_TOKEN")
    if not token or len(token) < 32:
        problems.append("EVALRY_JUDGE_TOKEN is required and must be at least 32 characters.")
    allowed = frozenset((get("EVALRY_JUDGE_ALLOWED_SENSITIVITY") or "public,internal").split(","))
    if not allowed <= set(_LEVELS):
        problems.append(f"EVALRY_JUDGE_ALLOWED_SENSITIVITY must list some of {', '.join(_LEVELS)}.")
    port = get("EVALRY_JUDGE_PORT") or "8002"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        problems.append("EVALRY_JUDGE_PORT must be a number from 1 to 65535.")
        port = "8002"
    if problems:
        raise ConfigError("Invalid configuration:\n- " + "\n- ".join(problems))
    assert model and token
    return Settings(
        model=model, rubric=rubric, rates=Rates(usd_per_mtok_in=rates[0], usd_per_mtok_out=rates[1]), token=token,
        allowed=allowed, host=get("EVALRY_JUDGE_HOST") or "127.0.0.1", port=int(port),
    )  # fmt: skip


def main() -> None:
    try:
        s = load_settings(os.environ)
    except ConfigError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    app = create_judge_app(AnthropicJudge(s.model, s.rubric, s.rates), token=s.token, allowed=s.allowed)
    uvicorn.run(app, host=s.host, port=s.port, access_log=False)
