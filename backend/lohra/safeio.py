"""Safe filesystem reads for UNTRUSTED project files (Fase 9).

Project instruction/skill files come from whatever repo Lohra is run in. Reading
them must not let a hostile repo OOM the process, hang on a FIFO/device, or
exfiltrate a secret via a symlink. ``read_text_bounded`` is the one chokepoint:
it rejects symlinks and non-regular files and reads at most ``max_bytes`` — so a
giant/FIFO/symlinked file can't break or leak. Never raises.
"""

from __future__ import annotations

from pathlib import Path


def read_text_bounded(path: Path, max_bytes: int) -> str | None:
    """Read up to ``max_bytes`` of a regular, non-symlink file as UTF-8 (errors
    replaced). None for missing/symlink/non-regular/unreadable. Never raises."""
    try:
        if path.is_symlink() or not path.is_file():  # no symlink-escape, no FIFO/device
            return None
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return None
    return raw[:max_bytes].decode("utf-8", errors="replace")
