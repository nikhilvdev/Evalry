"""Generator service: the only process that holds the LLM API key (plan §3, §7, §9).

Workers call ``POST /v1/generate`` with a shared bearer token and get a ``Generation`` back, so a runner or worker host never
sees a provider key. Data classification gates what may leave: a task whose sensitivity is not in the allowed set is refused
before anything is sent to the provider. Prompt and completion text are never logged. Configuration comes from the
environment and every problem is reported at once, never echoing a secret. The server binds to loopback unless told otherwise;
put TLS in front of it.
"""

import hmac
import json
import os
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import anthropic
import uvicorn
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from logquill import bind_context
from pydantic import BaseModel, ConfigDict, Field

from evalry.agent.claude import AnthropicAgent
from evalry.agent.loop import AgentModel, AgentTurn
from evalry.billing.cost import Rates
from evalry.generation.base import Generation, Generator
from evalry.generation.claude import AnthropicGenerator, GenerationRefused
from evalry.log import get_logger

log = get_logger("generator-service")

_LEVELS = ("public", "internal", "confidential", "restricted")
_MAX_PROMPT = 200_000


class ConfigError(Exception):
    """One or more settings are missing or unsafe. The message lists all of them."""


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=_MAX_PROMPT)
    sample: int = Field(ge=0)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"


class AgentStepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: str = Field(min_length=1, max_length=20_000)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=400)
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"


_MAX_AGENT_BYTES = 4 * 1024 * 1024


def create_generator_app(
    generator: Generator, *, token: str, allowed: frozenset[str], agent: AgentModel | None = None
) -> FastAPI:
    app = FastAPI(title="Evalry generator", version="1.0.0", docs_url=None, redoc_url=None)

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

    class _Refuse(Exception):
        def __init__(self, status: int, message: str, retry_after: str | None = None) -> None:
            self.status, self.message, self.retry_after = status, message, retry_after

    @app.exception_handler(_Refuse)
    async def _refused(request: Request, exc: _Refuse) -> JSONResponse:
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        return JSONResponse({"error": exc.message}, status_code=exc.status, headers=headers)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/generate", dependencies=[Depends(authed)])
    def generate(body: GenerateRequest) -> Generation:
        if body.sensitivity not in allowed:
            raise _Refuse(
                403,
                f"Data classified {body.sensitivity!r} may not go to this provider (allowed: {sorted(allowed)}). "
                "Use an approved provider or reclassify the task.",
            )
        try:
            out = generator.generate(body.prompt, sample=body.sample)
        except GenerationRefused as e:
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
        log.info(
            "generated",
            sample=body.sample,
            model=out.model,
            tokens_in=out.tokens_in,
            tokens_out=out.tokens_out,
        )
        return out

    @app.post("/v1/agent-step", dependencies=[Depends(authed)])
    def agent_step(body: AgentStepRequest) -> AgentTurn:
        """One model step for a tier 4 agent. The tool set is fixed here (``evalry.agent.tools``), never taken from the caller."""
        if agent is None:
            raise _Refuse(501, "This service was started without agent support.")
        if body.sensitivity not in allowed:
            raise _Refuse(
                403,
                f"Data classified {body.sensitivity!r} may not go to this provider (allowed: {sorted(allowed)}).",
            )
        if len(json.dumps(body.messages)) > _MAX_AGENT_BYTES:
            raise _Refuse(
                413, "The conversation is over 4 MB. The agent's step limit should have ended the run sooner."
            )
        try:
            out = agent.step(body.system, body.messages)
        except anthropic.RateLimitError as e:
            raise _Refuse(
                429,
                "The provider rate-limited this request. Retry shortly.",
                e.response.headers.get("retry-after", "30"),
            ) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            log.error("provider error", error=type(e).__name__, status=getattr(e, "status_code", None))
            raise _Refuse(502, "The provider failed this request. Retry shortly.") from e
        log.info(
            "agent step",
            model=out.model,
            tokens_in=out.tokens_in,
            tokens_out=out.tokens_out,
            stop_reason=out.stop_reason,
        )
        return out

    return app


@dataclass(frozen=True)
class Settings:
    model: str
    rates: Rates
    token: str
    allowed: frozenset[str]
    host: str
    port: int


def load_settings(env: Mapping[str, str]) -> Settings:
    problems: list[str] = []

    def get(name: str) -> str | None:
        return env.get(name) or None

    model = get("EVALRY_GEN_MODEL")
    if not model:
        problems.append(
            "EVALRY_GEN_MODEL is required (for example claude-sonnet-5-5); there is no default model."
        )
    rates: list[float] = []
    for name in ("EVALRY_GEN_USD_PER_MTOK_IN", "EVALRY_GEN_USD_PER_MTOK_OUT"):
        try:
            rates.append(float(get(name) or "x"))
            if rates[-1] < 0:
                raise ValueError
        except ValueError:
            problems.append(
                f"{name} is required: the provider price in USD per million tokens (no defaults)."
            )
            rates.append(0.0)
    token = get("EVALRY_GEN_TOKEN")
    if not token or len(token) < 32:
        problems.append(
            "EVALRY_GEN_TOKEN is required and must be at least 32 characters (the workers' shared secret)."
        )
    allowed = frozenset((get("EVALRY_GEN_ALLOWED_SENSITIVITY") or "public,internal").split(","))
    if not allowed <= set(_LEVELS):
        problems.append(f"EVALRY_GEN_ALLOWED_SENSITIVITY must list some of {', '.join(_LEVELS)}.")
    port = get("EVALRY_GEN_PORT") or "8001"
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        problems.append("EVALRY_GEN_PORT must be a number from 1 to 65535.")
        port = "8001"
    if problems:
        raise ConfigError("Invalid configuration:\n- " + "\n- ".join(problems))
    assert model and token
    return Settings(
        model=model,
        rates=Rates(usd_per_mtok_in=rates[0], usd_per_mtok_out=rates[1]),
        token=token,
        allowed=allowed,
        host=get("EVALRY_GEN_HOST") or "127.0.0.1",
        port=int(port),
    )


def main() -> None:
    try:
        s = load_settings(os.environ)
    except ConfigError as e:
        print(e, file=sys.stderr)
        raise SystemExit(2) from e
    app = create_generator_app(
        AnthropicGenerator(s.model, s.rates),
        token=s.token,
        allowed=s.allowed,
        agent=AnthropicAgent(s.model, s.rates),
    )
    uvicorn.run(app, host=s.host, port=s.port, access_log=False)
