"""Intra-message FIFO for known file tools; no process-global locks or policy.

Only path metadata is consulted. Original calls/arguments still reach dispatch
and its authorization gates unchanged. Identity is a planning-time snapshot:
external symlink changes, hard links and other sessions/writers are not ordered.
"""

import os
from pathlib import Path

from lohra.agent.types import ToolCall
from lohra.providers.transports.base import parse_tool_arguments

_FILE_TOOLS = frozenset({"read_file", "write_file"})


def _file_key(call: ToolCall) -> str | None:
    path = parse_tool_arguments(call.arguments).get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        # Do not expand '~': the file handlers use it literally, too. Resolve
        # symlinks before '..', and allow a not-yet-created file/parent suffix.
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        # Identity failure must not replace a dispatch error or reveal a path.
        return None


def tool_call_batches(calls: tuple[ToolCall, ...]) -> tuple[tuple[int, ...], ...]:
    """One sequential index queue per resource, in emitted order.

    If any file identity is unknown, serialize all file calls conservatively;
    unrelated tools still run in parallel. All state is bounded by this batch.
    """
    keys = {i: _file_key(call) for i, call in enumerate(calls) if call.name in _FILE_TOOLS}
    uncertain = any(key is None for key in keys.values())
    batches: dict[tuple, list[int]] = {}
    for index in range(len(calls)):
        key = ("file", None if uncertain else keys[index]) if index in keys else ("call", index)
        batches.setdefault(key, []).append(index)
    return tuple(tuple(indices) for indices in batches.values())
