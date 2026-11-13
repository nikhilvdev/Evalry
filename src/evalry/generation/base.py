"""Generator plugin interface (plan §3, §19) with a scripted fake for offline, free tests.

Generators see the task prompt only. Hidden tests never reach this module, and prompt/completion text is never logged.
"""

import re
import time
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from evalry.log import get_logger

log = get_logger("generation")


class Generation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    model: str = Field(description="Identifier the provider returned, not the one requested (plan §19).")
    tokens_in: int = Field(ge=0, default=0)
    tokens_out: int = Field(ge=0, default=0)
    cost_usd: float = Field(ge=0, default=0)


class Generator(Protocol):
    def generate(self, prompt: str, *, sample: int) -> Generation: ...


class FakeGenerator:
    """Returns scripted responses in order, one per call; never touches the network."""

    def __init__(self, responses: Sequence[str], *, model: str = "fake-model-1") -> None:
        self._responses = list(responses)
        self._model = model
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, sample: int) -> Generation:
        if len(self.prompts) >= len(self._responses):
            raise RuntimeError(
                f"FakeGenerator has {len(self._responses)} scripted responses; call {len(self.prompts) + 1} has none."
            )
        text = self._responses[len(self.prompts)]
        self.prompts.append(prompt)
        return Generation(
            text=text, model=self._model, tokens_in=len(prompt.split()), tokens_out=len(text.split())
        )


def generate_logged(generator: Generator, prompt: str, *, sample: int) -> Generation:
    """Call the generator and log model, tokens and cost (LogQuill ``llm_call``), never the text."""
    start = time.perf_counter()
    out = generator.generate(prompt, sample=sample)
    log.llm_call(
        model=out.model,
        tokens_in=out.tokens_in,
        tokens_out=out.tokens_out,
        cost_usd=out.cost_usd,
        latency_ms=(time.perf_counter() - start) * 1000,
        sample=sample,
    )
    return out


_FENCE = re.compile(r"```[ \t]*(?:[\w+-]+)?[ \t]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    """Pull the solution out of a model reply.

    Takes the last fenced block (models put the final answer last, after drafts), else the whole reply when there is no
    fence. An unterminated fence or empty reply yields "", which grades 0, rather than guessing.
    """
    blocks = _FENCE.findall(text)
    if blocks:
        return str(blocks[-1])
    return "" if text.lstrip().startswith("```") else text.strip()
