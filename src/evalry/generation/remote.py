"""Client for the generator service: how a worker generates without ever holding an LLM key (plan §3).

The prompt and the service token cross this connection, so plain http is allowed to loopback only.
"""

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from evalry.generation.base import Generation

Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def require_https(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" and not (
        parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
    ):
        raise ValueError(f"Service URL {url!r} must be https (http is allowed for localhost only).")


class RemoteGenerator:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        sensitivity: str = "internal",
        timeout_s: float = 120,
        post: Post = _urllib_post,
    ) -> None:
        require_https(url)
        self._url = url.rstrip("/") + "/v1/generate"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._sensitivity, self._timeout, self._post = sensitivity, timeout_s, post

    def generate(self, prompt: str, *, sample: int) -> Generation:
        body = json.dumps({"prompt": prompt, "sample": sample, "sensitivity": self._sensitivity}).encode()
        status, raw = self._post(self._url, self._headers, body, self._timeout)
        if status != 200:
            try:
                detail = json.loads(raw).get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Generator service returned {status}: {detail or 'no detail'}")
        return Generation.model_validate_json(raw)
