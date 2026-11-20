"""Client for the generator service's agent endpoint: how a worker steps an agent without holding an LLM key."""

import json
from typing import Any

from evalry.agent.loop import AgentTurn
from evalry.generation.remote import Post, _urllib_post, require_https


class RemoteAgent:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        sensitivity: str = "internal",
        timeout_s: float = 300,
        post: Post = _urllib_post,
    ) -> None:
        require_https(url)
        self._url = url.rstrip("/") + "/v1/agent-step"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._sensitivity, self._timeout, self._post = sensitivity, timeout_s, post

    def step(self, system: str, messages: list[dict[str, Any]]) -> AgentTurn:
        body = json.dumps({"system": system, "messages": messages, "sensitivity": self._sensitivity}).encode()
        status, raw = self._post(self._url, self._headers, body, self._timeout)
        if status != 200:
            try:
                detail = json.loads(raw).get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Generator service returned {status}: {detail or 'no detail'}")
        return AgentTurn.model_validate_json(raw)
