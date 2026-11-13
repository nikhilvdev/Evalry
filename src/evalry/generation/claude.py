"""Anthropic Messages API generator (plan §3, §19). Runs only inside the generator service, which holds the API key.

The model is always the one configured, never a silent fallback: server-side fallbacks are off, so a safety refusal is an
error the job records, not a different model's answer. ``Generation.model`` is what the API returned, which is what lands in
the sample result. No ``temperature``: current models reject non-default sampling, so samples are independent draws.
"""

import anthropic

from evalry.billing.cost import Rates
from evalry.generation.base import Generation


class GenerationRefused(RuntimeError):
    """The model declined for safety reasons. Retrying the same prompt will not help."""


class AnthropicGenerator:
    def __init__(
        self,
        model: str,
        rates: Rates,
        *,
        client: anthropic.Anthropic | None = None,
        max_tokens: int = 16000,
    ) -> None:
        self._model, self._rates, self._max_tokens = model, rates, max_tokens
        self._client = (
            client or anthropic.Anthropic()
        )  # key from ANTHROPIC_API_KEY (or an `ant auth login` profile)

    def generate(self, prompt: str, *, sample: int) -> Generation:
        msg = self._client.messages.create(
            model=self._model, max_tokens=self._max_tokens, messages=[{"role": "user", "content": prompt}]
        )
        if msg.stop_reason == "refusal":
            category = msg.stop_details.category if msg.stop_details else None
            raise GenerationRefused(
                f"{msg.model} declined the prompt (category: {category}). Revise the task prompt."
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        t_in, t_out = msg.usage.input_tokens, msg.usage.output_tokens
        cost = (t_in * self._rates.usd_per_mtok_in + t_out * self._rates.usd_per_mtok_out) / 1_000_000
        return Generation(text=text, model=msg.model, tokens_in=t_in, tokens_out=t_out, cost_usd=cost)
