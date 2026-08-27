"""Write ``~/.lohra/.env`` — the half ``config/env_file`` never had (ONB-3).

``config.env_file`` only *reads*; the only writer in the whole project was Rust
(``desktop/src-tauri/src/config.rs``), so a CLI wizard had no way to persist what
it collected. This is the symmetric Python writer, and it is deliberately
conservative because the file holds API keys:

* **Upsert, never rewrite.** Unrelated lines and comments survive verbatim.
* **Idempotent.** A value that is already effective is not written at all, so
  running the wizard twice leaves the file byte-identical.
* **Deduplicating.** ``parse_env_text`` is last-wins, so a *later* duplicate of a
  key would silently beat the line we just edited; duplicates collapse to one.
* **Atomic + owner-only.** tmp file in the same directory, ``os.replace``, 0600
  (best-effort: on Windows chmod is a no-op, which is known and documented).
* **Round-trippable.** Everything emitted here parses back through
  ``parse_env_text`` to the exact value that went in.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from lohra.config.env_file import parse_env_text

_NEEDS_QUOTES = (" ", "\t", "#", "'", '"')


def format_value(value: str) -> str:
    """Render ``value`` so ``parse_env_text`` reads back exactly this string."""
    if value and not any(char in value for char in _NEEDS_QUOTES) and value == value.strip():
        return value
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    return value  # both quote styles present: emit raw (keys never look like this)


def upsert_env_file(path: str | Path, updates: Mapping[str, str]) -> tuple[str, ...]:
    """Set each ``KEY=value`` in ``path``; return the keys actually written.

    A key whose current parsed value already equals the requested one is skipped,
    so an all-defaults wizard run writes nothing and touches no bytes.
    """
    file = Path(path)
    text = _read(file)
    current = parse_env_text(text)
    changed = {key: value for key, value in updates.items() if current.get(key) != value}
    if not changed:
        return ()

    _write_atomic(file, _render(text, changed))
    return tuple(changed)


def _read(file: Path) -> str:
    try:
        return file.read_text(encoding="utf-8")
    except OSError:  # missing, unreadable, a directory — start from empty
        return ""


def _render(text: str, changed: Mapping[str, str]) -> str:
    """The new file body: edit in place where a key already lives, append the rest."""
    seen: set[str] = set()
    lines: list[str] = []
    for raw in text.splitlines():
        key = _line_key(raw)
        if key is None or key not in changed:
            lines.append(raw)
            continue
        if key in seen:
            continue  # collapse a duplicate definition instead of leaving it to win
        seen.add(key)
        lines.append(f"{key}={format_value(changed[key])}")
    for key, value in changed.items():
        if key not in seen:
            lines.append(f"{key}={format_value(value)}")
    return "\n".join(lines) + "\n"


def _line_key(raw: str) -> str | None:
    """The key a line defines, or None for blanks/comments — mirrors parse_env_text."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export ") :]
    key = line.partition("=")[0].strip()
    return key or None


def _write_atomic(file: Path, body: str) -> None:
    """Replace ``file`` in one step, owner-only, leaving no partial file behind."""
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        _chmod_600(tmp)
        os.replace(tmp, file)
    finally:
        try:
            tmp.unlink()  # only survives if replace never happened
        except OSError:
            pass
    _chmod_600(file)


def _chmod_600(file: Path) -> None:
    try:
        file.chmod(0o600)
    except OSError:  # Windows / exotic filesystems: no restricted permission
        pass
