"""Filesystem tools: read_file, write_file (toolset "file").

Handlers return JSON-string envelopes (tool_result / tool_error) and never
raise into the dispatcher. They self-register into the singleton registry on
import.

⚠️  These tools are UNSANDBOXED: they read and write any path the process can
access, with the operator's full privileges, and (unlike the terminal tool)
are not behind the approval gate. That is intentional for a local single-user
agent — file access is the point. Filesystem isolation against untrusted use
is the container/ssh backend's responsibility (spec §7), not these handlers.
Run Lohra only on inputs you trust at the privilege level it runs with.
"""

from __future__ import annotations

import pathlib
from typing import Any

from lohra.tools.registry import registry, tool_error, tool_result

_MAX_READ_CHARS = 100_000


def read_file(args: dict[str, Any], **_kwargs: Any) -> str:
    path = args.get("path")
    if not path:
        return tool_error("missing required argument 'path'")
    try:
        with open(path, encoding="utf-8") as handle:
            content = handle.read(_MAX_READ_CHARS + 1)
    except FileNotFoundError:
        return tool_error(f"file not found: {path}")
    except IsADirectoryError:
        return tool_error(f"path is a directory: {path}")
    except UnicodeDecodeError:
        return tool_error(f"file is not valid UTF-8 text: {path}")
    except OSError as exc:
        return tool_error(f"could not read {path}: {exc}")

    truncated = len(content) > _MAX_READ_CHARS
    return tool_result(content[:_MAX_READ_CHARS], truncated=truncated, path=str(path))


def write_file(args: dict[str, Any], **_kwargs: Any) -> str:
    path = args.get("path")
    content = args.get("content")
    if not path:
        return tool_error("missing required argument 'path'")
    if content is None:
        return tool_error("missing required argument 'content'")
    if not isinstance(content, str):
        return tool_error("'content' must be a string")
    try:
        target = pathlib.Path(path)
        if target.parent and not target.parent.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return tool_error(f"could not write {path}: {exc}")
    return tool_result(bytes_written=len(content.encode("utf-8")), path=str(path))


_READ_SCHEMA = {
    "description": "Read a UTF-8 text file from the local filesystem.",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path to the file"}},
        "required": ["path"],
    },
}

_WRITE_SCHEMA = {
    "description": "Write a UTF-8 text file (creating parent directories).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file"},
            "content": {"type": "string", "description": "Full file contents to write"},
        },
        "required": ["path", "content"],
    },
}

registry.register("read_file", "file", _READ_SCHEMA, read_file, emoji="📄")
registry.register("write_file", "file", _WRITE_SCHEMA, write_file, emoji="✍️")
