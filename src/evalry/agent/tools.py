"""The agent's fixed tool set (plan §4 tier 4). Defined here, not by the caller, so the service never accepts arbitrary tools."""

from typing import Any

TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": "Run a shell command in the repository root. No network. Output is truncated; commands time out after 60 seconds.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "read_file",
        "description": "Read a text file by path relative to the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a text file by path relative to the repository root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
