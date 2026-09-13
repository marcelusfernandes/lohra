"""Internal sandbox observations; the public tool result remains a JSON string.

Only the sandbox decision constructs this marker. Tool/provider prose, even a
JSON object claiming ``denied: true``, is never classified as a policy refusal.
Closed tool categories/reasons also bound the counters and exclude caller text.
"""

from collections import Counter
from dataclasses import dataclass
from threading import Lock
from typing import Any

from lohra.tools.registry import tool_error

DENIAL_REASONS = {
    "fs_outside_scope": "path is outside the workflow working scope (sandbox denied)",
    "fs_read_only": "path is under a read-only workflow root (sandbox denied the write)",
    "egress_not_allowed": "host is not in the workflow egress allowlist (sandbox denied)",
    "terminal_disabled": "the 'terminal' tool is disabled for workflow leaves (sandbox denied)",
    "mcp_not_allowed": "MCP tool is not in the workflow leaf allowlist (sandbox denied)",
    "tainted_fs": "tainted run: filesystem access is disabled for leaves",
    "tainted_egress": "tainted run: web egress is disabled for leaves",
    "tainted_terminal": "tainted run: shell access is disabled for leaves",
    "tainted_mcp": "tainted run: MCP tools are disabled for leaves",
}
DENIAL_TOOLS = frozenset({"read_file", "write_file", "web_fetch", "web_search", "terminal", "mcp"})


@dataclass(frozen=True)
class Denial:
    tool: str
    reason: str


class SandboxDenied(str):
    """A string with trusted metadata, absent from JSON/wire serialization."""

    def __new__(cls, text: str, denial: Denial) -> "SandboxDenied":
        result = super().__new__(cls, text)
        result.denial = denial
        return result

    def __getnewargs__(self) -> tuple[str, Denial]:
        # SDKs/in-process consumers may copy request/message objects before
        # encoding them. Keep the normal string's copy protocol usable.
        return str(self), self.denial


def denied(name: str, reason: str, message: str | None = None) -> str:
    # An arbitrary MCP name can contain private caller text. Aggregate this
    # open-ended family as one category, just as the audit redacts its names.
    tool = name if name in DENIAL_TOOLS else "mcp"
    text = DENIAL_REASONS[reason] if message is None else message
    return SandboxDenied(tool_error(text), Denial(tool, reason))


def denial_of(result: Any) -> Denial | None:
    return result.denial if isinstance(result, SandboxDenied) else None


class DenialCounts:
    """One leaf's exact observations, independent of the optional audit sink."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: Counter[tuple[str, str]] = Counter()

    def observe(self, frame: dict[str, Any]) -> None:
        params = frame.get("params")
        if not isinstance(params, dict) or params.get("type") != "tool.complete":
            return
        payload = params.get("payload")
        result = denial_of(payload.get("result")) if isinstance(payload, dict) else None
        if result is not None:
            with self._lock:
                self._counts[(result.tool, result.reason)] += 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"tool": tool, "reason": reason, "count": count}
                for (tool, reason), count in sorted(self._counts.items())
            ]
