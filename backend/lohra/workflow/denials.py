"""Fold exact leaf observations into bounded per-node advisory counts (#89)."""

from collections import Counter
from typing import Any

from lohra.tools.sandbox_denials import DENIAL_REASONS, DENIAL_TOOLS


class DenialTally:
    """Owned by one engine, guarded by its result lock. No cost/terminal gate.

    A running leaf may be read again at timeout, on_done, and seal. Fold only
    each snapshot's positive delta; stale reads cannot lower the watermark.
    Keys are bounded by the run's leaf lifetime and the closed vocabulary.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], int] = {}
        self._counts: Counter[tuple[str, str, str]] = Counter()

    def fold(self, sub_id: str, node_id: str, rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            tool, reason, count = row.get("tool"), row.get("reason"), row.get("count")
            if (not isinstance(tool, str) or tool not in DENIAL_TOOLS
                    or not isinstance(reason, str) or reason not in DENIAL_REASONS
                    or type(count) is not int or count <= 0):
                continue
            key = (sub_id, tool, reason)
            previous = self._seen.get(key, 0)
            if count > previous:
                self._seen[key] = count
                self._counts[(node_id, tool, reason)] += count - previous

    def drain(self) -> list[str]:
        messages = [
            f"{node}: {count} tool calls denied by sandbox: {tool} — "
            f"{DENIAL_REASONS[reason]} (advisory)"
            for (node, tool, reason), count in sorted(self._counts.items())
        ]
        self._counts.clear()
        return messages
