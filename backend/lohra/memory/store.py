"""MemoryStore — declarative agent memory on disk (spec §2).

MEMORY.md (agent notes) and USER.md (user profile) live under HOME/memories/,
each a §-delimited list of entries with a whole-file character budget. Writes
are atomic (temp + fsync + os.replace) and re-read disk first so concurrent
edits are seen.

FROZEN SNAPSHOT (Invariante #1): load_snapshot() captures the rendered memory
once at session start; snapshot() returns that frozen text. Mid-session writes
update the disk immediately but never the snapshot, keeping the provider
prefix-cache warm. The snapshot refreshes only on the next load_snapshot().
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

ENTRY_DELIMITER = "\n§\n"
MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375

_SPLIT = re.compile(r"\n*§\n*")


class MemoryError(Exception):
    """Base for memory mutation failures."""


class MemoryLimitExceeded(MemoryError):
    """A write would push the file over its character budget."""


class EntryNotFound(MemoryError):
    """No entry contains the given substring."""


class AmbiguousEntry(MemoryError):
    """More than one entry contains the given substring."""


def _parse(content: str) -> list[str]:
    if not content.strip():
        return []
    return [entry.strip() for entry in _SPLIT.split(content.strip()) if entry.strip()]


def _render(entries: list[str]) -> str:
    return ENTRY_DELIMITER.join(entries)


def _find_unique(entries: list[str], substring: str) -> int:
    matches = [i for i, entry in enumerate(entries) if substring in entry]
    if not matches:
        raise EntryNotFound(f"no memory entry contains {substring!r}")
    if len(matches) > 1:
        raise AmbiguousEntry(f"{len(matches)} entries contain {substring!r}; be more specific")
    return matches[0]


class MemoryFile:
    """One §-delimited memory file with atomic, char-bounded mutations."""

    def __init__(self, path: Path, char_limit: int) -> None:
        self.path = path
        self.char_limit = char_limit
        self._lock = threading.RLock()

    def _read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def entries(self) -> list[str]:
        return _parse(self._read())

    def render(self) -> str:
        return _render(self.entries())

    def add(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._lock:
            entries = _parse(self._read())  # re-read: mutate fresh state
            if text not in entries:
                entries.append(text)
            self._write(entries)

    def replace(self, old_text: str, new_text: str) -> None:
        with self._lock:
            entries = _parse(self._read())
            entries[_find_unique(entries, old_text)] = new_text.strip()
            self._write(entries)

    def remove(self, old_text: str) -> None:
        with self._lock:
            entries = _parse(self._read())
            del entries[_find_unique(entries, old_text)]
            self._write(entries)

    def _write(self, entries: list[str]) -> None:
        content = _render(entries)
        if len(content) > self.char_limit:
            raise MemoryLimitExceeded(
                f"memory would be {len(content)} chars, over the {self.char_limit} budget"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)


class MemoryStore:
    """Both memory files plus the per-session frozen snapshot."""

    def __init__(self, home: Path) -> None:
        memories = home / "memories"
        self.memory = MemoryFile(memories / "MEMORY.md", MEMORY_CHAR_LIMIT)
        self.user = MemoryFile(memories / "USER.md", USER_CHAR_LIMIT)
        self._snapshot: dict[str, str] | None = None

    def file_for(self, target: str) -> MemoryFile:
        """'user' -> USER.md, anything else -> MEMORY.md."""
        return self.user if target == "user" else self.memory

    def load_snapshot(self) -> None:
        """Capture the rendered memory for this session (frozen until next call)."""
        self._snapshot = {"memory": self.memory.render(), "user": self.user.render()}

    def snapshot(self) -> dict[str, str]:
        if self._snapshot is None:
            self.load_snapshot()
        assert self._snapshot is not None
        return self._snapshot
