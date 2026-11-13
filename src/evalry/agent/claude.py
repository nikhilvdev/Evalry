"""Anthropic backend for agent steps. Runs only inside the generator service, which holds the API key.

Same rules as the generator: the configured model, no fallback, no sampling parameters. The assistant's content blocks are
returned exactly as the API produced them (thinking blocks included), because the next request must send them back unchanged.
"""

from typing import Any

import anthropic

from evalry.agent.loop import AgentTurn
from evalry.agent.tools import TOOLS
from evalry.billing.cost import Rates


class AnthropicAgent:
    def __init__(
        self, model: str, rates: Rates, *, client: anthropic.Anthropic | None = None, max_tokens: int = 16000
    ) -> None:
        self._model, self._rates, self._max_tokens = model, rates, max_tokens
        self._client = client or anthropic.Anthropic()

    def step(self, system: str, messages: list[dict[str, Any]]) -> AgentTurn:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            tools=TOOLS,  # type: ignore[arg-type]  # our dicts match ToolParam; the SDK's union is too wide to infer
            messages=messages,  # type: ignore[arg-type]
        )
        t_in, t_out = msg.usage.input_tokens, msg.usage.output_tokens
        return AgentTurn(
            content=[b.model_dump(mode="json", exclude_none=True) for b in msg.content], stop_reason=msg.stop_reason, model=msg.model,
            tokens_in=t_in, tokens_out=t_out,
            cost_usd=(t_in * self._rates.usd_per_mtok_in + t_out * self._rates.usd_per_mtok_out) / 1_000_000,
        )  # fmt: skip
