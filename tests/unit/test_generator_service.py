from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from evalry.billing.cost import Rates
from evalry.cli import main
from evalry.generation.base import FakeGenerator
from evalry.generation.claude import AnthropicGenerator, GenerationRefused
from evalry.generation.remote import RemoteGenerator
from evalry.generation.service import ConfigError, create_generator_app, load_settings

TOKEN = "t" * 40
RATES = Rates(usd_per_mtok_in=2.0, usd_per_mtok_out=10.0)


class StubMessages:
    """Stands in for the Anthropic client: no network, no key, no spend."""

    def __init__(self, stop_reason: str = "end_turn", error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.stop_reason, self.error = stop_reason, error

    def create(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(
            model="claude-returned-by-provider", stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text="def f(): pass")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=500),
        )  # fmt: skip


def gen(stub: StubMessages) -> AnthropicGenerator:
    return AnthropicGenerator("claude-requested", RATES, client=SimpleNamespace(messages=stub))  # type: ignore[arg-type]


def client(g: Any, allowed: frozenset[str] = frozenset({"public", "internal"})) -> TestClient:
    return TestClient(create_generator_app(g, token=TOKEN, allowed=allowed))


AUTH = {"Authorization": f"Bearer {TOKEN}"}
BODY = {"prompt": "write f", "sample": 0}


def test_generator_records_returned_model_tokens_and_cost_and_sends_only_the_prompt() -> None:
    stub = StubMessages()
    out = gen(stub).generate("write f", sample=0)
    assert (out.model, out.text, out.tokens_in, out.tokens_out) == (
        "claude-returned-by-provider",
        "def f(): pass",
        1000,
        500,
    )
    assert out.cost_usd == pytest.approx(0.007)  # 1000 * 2/1M + 500 * 10/1M
    (call,) = stub.calls
    assert call["model"] == "claude-requested" and call["messages"] == [
        {"role": "user", "content": "write f"}
    ]
    assert (
        not {"temperature", "fallbacks", "betas"} & call.keys()
    )  # no sampling knobs, never a silent fallback model


def test_refusal_is_an_error_not_a_fallback() -> None:
    with pytest.raises(GenerationRefused, match="cyber"):
        gen(StubMessages("refusal")).generate("p", sample=0)


def test_service_needs_the_token_and_blocks_classified_data() -> None:
    c = client(gen(StubMessages()))
    assert c.post("/v1/generate", json=BODY).status_code == 401
    assert c.post("/v1/generate", json=BODY, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.post("/v1/generate", json=BODY, headers=AUTH).json()["model"] == "claude-returned-by-provider"
    stub = StubMessages()
    blocked = client(gen(stub)).post("/v1/generate", json={**BODY, "sensitivity": "restricted"}, headers=AUTH)
    assert (
        blocked.status_code == 403 and "restricted" in blocked.json()["error"] and stub.calls == []
    )  # nothing left the building


def test_service_maps_provider_failures_to_retryable_statuses() -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    limited = anthropic.RateLimitError(
        "slow", response=httpx2.Response(429, headers={"retry-after": "7"}, request=req), body=None
    )
    down = anthropic.APIConnectionError(request=req)
    r = client(gen(StubMessages(error=limited))).post("/v1/generate", json=BODY, headers=AUTH)
    assert (r.status_code, r.headers["retry-after"]) == (429, "7")
    assert (
        client(gen(StubMessages(error=down))).post("/v1/generate", json=BODY, headers=AUTH).status_code == 502
    )
    assert (
        client(gen(StubMessages("refusal"))).post("/v1/generate", json=BODY, headers=AUTH).status_code == 422
    )


def test_remote_generator_round_trip_and_error_reporting() -> None:
    c = client(gen(StubMessages()))

    def post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        r = c.post("/v1/generate", content=body, headers=headers)
        return r.status_code, r.content

    out = RemoteGenerator("http://127.0.0.1:8001", TOKEN, post=post).generate("write f", sample=2)
    assert out.text == "def f(): pass" and out.cost_usd > 0
    with pytest.raises(RuntimeError, match="401"):
        RemoteGenerator("http://localhost:8001", "wrong", post=post).generate("p", sample=0)
    with pytest.raises(RuntimeError, match="restricted"):
        RemoteGenerator("http://localhost:8001", TOKEN, sensitivity="restricted", post=post).generate(
            "p", sample=0
        )
    for bad in ("http://gen.internal:8001", "file:///etc/passwd", "ftp://x"):
        with pytest.raises(ValueError, match="https"):
            RemoteGenerator(bad, TOKEN)


def test_prompt_reply_and_token_never_reach_the_logs(capfd: pytest.CaptureFixture[str]) -> None:
    c = client(FakeGenerator(["SECRET_REPLY_TEXT"]))
    c.post("/v1/generate", json={"prompt": "SECRET_PROMPT_TEXT", "sample": 0}, headers=AUTH)
    c.post("/v1/generate", json=BODY, headers={"Authorization": "Bearer WRONG_TOKEN_VALUE"})
    out = capfd.readouterr()
    assert not {"SECRET_PROMPT_TEXT", "SECRET_REPLY_TEXT", TOKEN, "WRONG_TOKEN_VALUE"} & {
        w for w in (out.out + out.err).replace('"', " ").split() if w
    }
    assert "SECRET_PROMPT_TEXT" not in out.out + out.err and "SECRET_REPLY_TEXT" not in out.out + out.err


def test_settings_report_every_problem_and_need_explicit_model_and_rates() -> None:
    with pytest.raises(ConfigError) as e:
        load_settings({})
    for name in (
        "EVALRY_GEN_MODEL",
        "EVALRY_GEN_USD_PER_MTOK_IN",
        "EVALRY_GEN_USD_PER_MTOK_OUT",
        "EVALRY_GEN_TOKEN",
    ):
        assert name in str(e.value)
    ok = {
        "EVALRY_GEN_MODEL": "m",
        "EVALRY_GEN_USD_PER_MTOK_IN": "2",
        "EVALRY_GEN_USD_PER_MTOK_OUT": "10",
        "EVALRY_GEN_TOKEN": TOKEN,
    }
    s = load_settings(ok)
    assert (s.model, s.rates.usd_per_mtok_out, s.host, s.allowed) == (
        "m",
        10.0,
        "127.0.0.1",
        {"public", "internal"},
    )
    with pytest.raises(ConfigError, match="ALLOWED_SENSITIVITY"):
        load_settings({**ok, "EVALRY_GEN_ALLOWED_SENSITIVITY": "secret"})


def test_worker_needs_exactly_one_generator_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("EVALRY_DB_WORKER_DSN", "host=nowhere")
    monkeypatch.delenv("EVALRY_GEN_TOKEN", raising=False)
    replies = tmp_path / "r.json"
    replies.write_text("{}")
    assert main(["worker", "--once"]) == 2
    assert main(["worker", "--once", "--replies", str(replies), "--generator-url", "https://g"]) == 2
    assert main(["worker", "--once", "--generator-url", "https://g"]) == 2  # no token


def test_worker_judge_needs_its_token_and_a_sane_weight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("EVALRY_DB_WORKER_DSN", "host=nowhere")
    monkeypatch.delenv("EVALRY_JUDGE_TOKEN", raising=False)
    replies = tmp_path / "r.json"
    replies.write_text("{}")
    base = ["worker", "--once", "--replies", str(replies)]
    assert main([*base, "--judge-url", "https://j"]) == 2
    monkeypatch.setenv("EVALRY_JUDGE_TOKEN", "t")
    assert main([*base, "--judge-weight", "-1"]) == 2
