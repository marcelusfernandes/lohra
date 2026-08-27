"""SOUL.md — the agent persona (spec §3).

If HOME/SOUL.md exists and is non-empty, its content becomes the identity in
the stable tier (slot #1), replacing the default. Absent or empty -> the agent
falls back to the built-in identity.
"""

from __future__ import annotations

from pathlib import Path


def soul_path(home: Path) -> Path:
    return home / "SOUL.md"


def load_soul(home: Path) -> str | None:
    """Return the SOUL.md persona text, or None when absent/empty."""
    try:
        content = soul_path(home).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return content or None
