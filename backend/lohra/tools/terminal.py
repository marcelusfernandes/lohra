"""Terminal tool: run a shell command locally, gated by the approval gate.

`shell=True` is intentional — running arbitrary shell commands is the tool's
purpose. The security boundary is the approval gate (spec §5): dangerous
commands require explicit user approval before they execute.
"""

from __future__ import annotations

import subprocess
from typing import Any

from lohra.tools.approval import approval
from lohra.tools.registry import registry, tool_error, tool_result

_DEFAULT_TIMEOUT_SECONDS = 30
_MAX_OUTPUT_CHARS = 50_000


def terminal(args: dict[str, Any], **_kwargs: Any) -> str:
    command = args.get("command")
    if not command or not isinstance(command, str):
        return tool_error("missing required argument 'command' (string)")

    if not approval.require(command):
        return tool_error("command was not approved by the user", command=command)

    timeout = args.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
    cwd = args.get("cwd")
    try:
        proc = subprocess.run(
            command,
            shell=True,  # noqa: S602 — running shell commands is this tool's job
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"command timed out after {timeout}s", command=command)
    except OSError as exc:
        return tool_error(f"could not run command: {exc}", command=command)

    return tool_result(
        stdout=proc.stdout[:_MAX_OUTPUT_CHARS],
        stderr=proc.stderr[:_MAX_OUTPUT_CHARS],
        exit_code=proc.returncode,
    )


_SCHEMA = {
    "description": (
        "Run a shell command on the local machine and return stdout, stderr, "
        "and the exit code. Dangerous commands require user approval."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
        },
        "required": ["command"],
    },
}

registry.register("terminal", "terminal", _SCHEMA, terminal, emoji="💻")
