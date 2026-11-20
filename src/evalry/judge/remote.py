"""Client for the judge service: how a worker judges without holding an LLM key. Same transport rules as RemoteGenerator."""

import json

from evalry.generation.remote import Post, _urllib_post, require_https
from evalry.judge.base import Verdict


class RemoteJudge:
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
        self._url = url.rstrip("/") + "/v1/judge"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._sensitivity, self._timeout, self._post = sensitivity, timeout_s, post

    def judge(self, prompt: str, code: str, *, sample: int) -> Verdict:
        body = json.dumps(
            {"prompt": prompt, "code": code, "sample": sample, "sensitivity": self._sensitivity}
        ).encode()
        status, raw = self._post(self._url, self._headers, body, self._timeout)
        if status != 200:
            try:
                detail = json.loads(raw).get("error", "")
            except ValueError:
                detail = ""
            raise RuntimeError(f"Judge service returned {status}: {detail or 'no detail'}")
        return Verdict.model_validate_json(raw)
