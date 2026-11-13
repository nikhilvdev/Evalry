"""Anthropic judge. Runs only inside the judge service, which holds the API key.

Same rules as the generator: the configured model, no fallback, no sampling parameters. The score comes back as schema-checked
JSON (``output_config.format``) and is validated again here, so a malformed verdict fails the job instead of scoring 0.
"""

import json

import anthropic

from evalry.billing.cost import Rates
from evalry.judge.base import SCHEMA, SYSTEM, JudgeRefused, Verdict, judge_version, user_prompt


class AnthropicJudge:
    def __init__(
        self,
        model: str,
        rubric: str,
        rates: Rates,
        *,
        client: anthropic.Anthropic | None = None,
        max_tokens: int = 4000,
    ) -> None:
        self._model, self._rubric, self._rates, self._max_tokens = model, rubric, rates, max_tokens
        self._version = judge_version(model, rubric)
        self._client = client or anthropic.Anthropic()

    @property
    def version(self) -> str:
        return self._version

    def judge(self, prompt: str, code: str, *, sample: int) -> Verdict:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=SYSTEM.format(rubric=self._rubric),
            messages=[{"role": "user", "content": user_prompt(prompt, code)}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if msg.stop_reason == "refusal":
            raise JudgeRefused(f"{msg.model} declined to judge this solution.")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            raw = json.loads(text)
            t_in, t_out = msg.usage.input_tokens, msg.usage.output_tokens
            return Verdict(
                score=raw["score"], rationale=str(raw["rationale"]), model=msg.model, judge_version=self._version,
                tokens_in=t_in, tokens_out=t_out,
                cost_usd=(t_in * self._rates.usd_per_mtok_in + t_out * self._rates.usd_per_mtok_out) / 1_000_000,
            )  # fmt: skip
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(
                f"The judge returned an invalid verdict ({type(e).__name__}); the job will be retried."
            ) from e
