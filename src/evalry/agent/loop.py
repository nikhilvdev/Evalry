"""Agent loop for tier 4 tasks (plan §4): a model works in a sandboxed repository using a fixed tool set, under hard limits.

The model talks through the generator service (the key never enters a worker or a runner); every tool runs inside the hardened
container. The loop ends on a reply without tool calls, or on a limit (steps, wall time, cost), a timed-out command, or a
repeated identical call. It records what plan §4 asks for (steps, tool calls, tokens, wall time, cost) and a failure category
for each way a run can go wrong. Tool output and file content are never logged. The agent's work is graded elsewhere, in a fresh
container from the snapshot plus the diff, so the hidden tests never exist where the agent can read them.
"""

import re
import time
from collections import Counter
from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from evalry.log import get_logger
from evalry.repo_tasks import MAX_EDIT_FILE_BYTES
from evalry.runner.sandbox import SandboxResult
from evalry.safepath import safe_relpath
from evalry.schemas import AgentMetrics, AgentSpec, Task

log = get_logger("agent")

TOOL_TIMEOUT_S = 60
MAX_TOOL_OUTPUT = 8_000
LOOP_REPEATS = 3
_UNSAFE = re.compile(
    r"\b(curl|wget|nc|ncat|ssh|scp|sudo|docker|mount|chroot)\b|rm\s+-\w*r\w*f?\w*\s+/(\s|$)|(pip3?|apt(-get)?|npm|yarn)\s+(install|add)"
    r"|git\s+(clone|remote)|/proc/|/etc/(shadow|passwd)|\.ssh",
)
_SKIP = {"python", "python3", "-m", "sh", "-c", "npm", "npx", "run", "go", "cargo", "mvn", "gradle"}


class AgentTurn(BaseModel):
    """One model reply. ``content`` is the assistant's blocks exactly as returned, to be sent back unchanged next step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: list[dict[str, Any]]
    stop_reason: str | None = None
    model: str = Field(description="Identifier the provider returned (plan §19).")
    tokens_in: int = Field(ge=0, default=0)
    tokens_out: int = Field(ge=0, default=0)
    cost_usd: float = Field(ge=0, default=0)

    @property
    def tool_calls(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class AgentModel(Protocol):
    def step(self, system: str, messages: list[dict[str, Any]]) -> AgentTurn: ...


class Workspace(Protocol):
    dead: bool

    def exec(
        self, argv: list[str], *, stdin: bytes | None = ..., timeout_s: float = ..., cwd: str = ...
    ) -> SandboxResult: ...
    def export(self) -> bytes: ...


class AgentRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: AgentMetrics
    model: str
    cost_usd: float
    workspace_lost: bool = False


def _tests_key(command: list[str]) -> str:
    return next((t for t in command if t not in _SKIP and not t.startswith("-")), command[0])


def system_prompt(task: Task) -> str:
    assert task.repo
    return (
        "You are fixing an issue in a repository checked out at /work/repo. You have the tools bash, read_file and write_file; "
        "there is no network. Explore, make the change, and run the project's tests with "
        f"`{' '.join(task.repo.test_command)}` before you finish. Some tests are hidden and will be run after you finish. "
        "When you are done, reply with a short summary and no tool call."
    )


def _execute(ws: Workspace, call: dict[str, Any]) -> tuple[str, bool, bool]:
    """Run one tool call. Returns (text for the model, is_error, command_timed_out). Bad input is an error result, not a crash."""
    name, args = call.get("name"), call.get("input") or {}
    try:
        if name == "bash":
            cmd = args["command"]
            if not isinstance(cmd, str) or len(cmd) > 10_000:
                raise ValueError("command must be a string of at most 10000 characters")
            r = ws.exec(["sh", "-c", cmd], timeout_s=TOOL_TIMEOUT_S)
            if r.timed_out:
                return (
                    f"The command timed out after {TOOL_TIMEOUT_S}s and the sandbox was reset. The run is over.",
                    True,
                    True,
                )
            return (
                f"{(r.stdout + r.stderr)[-MAX_TOOL_OUTPUT:]}\n[exit {r.exit_code}]",
                r.exit_code != 0,
                False,
            )
        if name == "read_file":
            r = ws.exec(["cat", "./" + safe_relpath(str(args["path"]))], timeout_s=TOOL_TIMEOUT_S)
            return (
                (r.stdout[-MAX_TOOL_OUTPUT:] if r.exit_code == 0 else f"cannot read: {r.stderr[:300]}"),
                r.exit_code != 0,
                r.timed_out,
            )
        if name == "write_file":
            content = args["content"]
            if not isinstance(content, str) or len(content.encode()) > MAX_EDIT_FILE_BYTES:
                raise ValueError("content must be a string under 1 MB")
            path = "./" + safe_relpath(str(args["path"]))
            r = ws.exec(
                ["sh", "-c", 'mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", path],
                stdin=content.encode(),
                timeout_s=TOOL_TIMEOUT_S,
            )
            return (
                ("written" if r.exit_code == 0 else f"cannot write: {r.stderr[:300]}"),
                r.exit_code != 0,
                r.timed_out,
            )
    except (KeyError, ValueError, TypeError) as e:
        return f"invalid tool call: {e}", True, False
    return f"unknown tool {name!r}", True, False


def run_agent(
    task: Task,
    model: AgentModel,
    ws: Workspace,
    spec: AgentSpec,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> AgentRun:
    assert task.repo
    start = clock()
    messages: list[dict[str, Any]] = [{"role": "user", "content": task.prompt}]
    steps = tokens_in = tokens_out = 0
    cost = 0.0
    calls: Counter[str] = Counter()
    categories: set[str] = set()
    recent: list[str] = []
    ran_tests, model_name = False, ""
    key = _tests_key(task.repo.test_command)
    finished = False
    while steps < spec.max_steps:
        if clock() - start > spec.max_wall_s:
            categories.add("time_limit")
            break
        if cost >= spec.max_cost_usd:
            categories.add("cost_limit")
            break
        turn = model.step(system_prompt(task), messages)
        steps += 1
        tokens_in, tokens_out, cost, model_name = (
            tokens_in + turn.tokens_in,
            tokens_out + turn.tokens_out,
            cost + turn.cost_usd,
            turn.model,
        )
        messages.append({"role": "assistant", "content": turn.content})
        if turn.stop_reason == "refusal":
            categories.add("model_refused")
            break
        if not turn.tool_calls:
            finished = True
            break
        results: list[dict[str, Any]] = []
        for call in turn.tool_calls:
            calls[str(call.get("name"))] += 1
            text, is_error, timed_out = _execute(ws, call)
            results.append(
                {"type": "tool_result", "tool_use_id": call.get("id"), "content": text, "is_error": is_error}
            )
            command = str((call.get("input") or {}).get("command", ""))
            if call.get("name") == "bash":
                ran_tests = ran_tests or key in command
                if _UNSAFE.search(command):
                    categories.add("unsafe_action")
            recent.append(f"{call.get('name')}:{call.get('input')}")
            if timed_out:
                categories.add("command_timeout")
        messages.append({"role": "user", "content": results})
        if "command_timeout" in categories:
            break
        if len(recent) >= LOOP_REPEATS and len(set(recent[-LOOP_REPEATS:])) == 1:
            categories.add("looped")
            break
    else:
        categories.add("step_limit")
    if not ran_tests:
        categories.add("never_ran_tests")
    wall = clock() - start
    log.info(
        "agent finished", steps=steps, tool_calls=sum(calls.values()), tokens_in=tokens_in, tokens_out=tokens_out,
        wall_s=round(wall, 1), cost_usd=cost, finished=finished, categories=",".join(sorted(categories)),
    )  # fmt: skip
    return AgentRun(
        metrics=AgentMetrics(
            steps=steps, tool_calls=dict(calls), tokens_in=tokens_in, tokens_out=tokens_out, wall_s=wall,
            finished=finished, failure_categories=sorted(categories),
        ),
        model=model_name, cost_usd=cost, workspace_lost=ws.dead,
    )  # fmt: skip
