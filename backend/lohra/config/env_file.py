"""Load ``~/.lohra/.env`` into the environment (hand-rolled, no deps).

A packaged app launched from the Finder/desktop does NOT inherit the shell's
exported variables, so the backend can't see ``ANTHROPIC_API_KEY`` etc. The
desktop writes provider keys to ``~/.lohra/.env``; the CLI loads them at startup.
Real environment variables always win — the file only fills what's missing.

Deliberately NOT python-dotenv: the frozen sidecar must stay dependency-light.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines. Skips blanks/comments, strips quotes, honors
    an optional ``export`` prefix; only the first ``=`` splits."""
    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        pairs[key] = _unquote(value)
    return pairs


def apply_env_file(
    path: str | Path, *, environ: MutableMapping[str, str] | None = None
) -> list[str]:
    """Load ``path`` and set each var that isn't already present. Returns the
    keys actually applied (a real env var takes precedence and is left intact).
    Missing/unreadable file is a no-op."""
    env = os.environ if environ is None else environ
    file = Path(path)
    if not file.is_file():
        return []
    try:
        text = file.read_text(encoding="utf-8")
    except OSError:
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text).items():
        if key not in env:  # a real env var (even empty) takes precedence
            env[key] = value
            applied.append(key)
    return applied
