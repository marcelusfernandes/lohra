"""Exportable skill kits — package data for OTHER agents, not for Lohra herself.

``lohra/skills/export/`` ships skills that teach an ORCHESTRATOR (Codex CLI,
Claude Code, scripts) how to drive Lohra — starting with ``use-lohra``. They are
deliberately NOT under ``builtin/``: the SkillStore must never index them into
Lohra's own skill surface (a skill about invoking the CLI would be noise to the
agent that IS the CLI). Surface: ``lohra skill export <name> [--to DIR]``.
"""

from __future__ import annotations

from pathlib import Path


def export_root() -> Path:
    """The packaged export-kit root — resolved by package location, never CWD."""
    return Path(__file__).resolve().parent / "export"


def list_exportable() -> list[str]:
    root = export_root()
    if not root.exists():
        return []
    return sorted(p.parent.name for p in root.glob("*/SKILL.md"))


def read_exportable(name: str) -> str:
    path = export_root() / name / "SKILL.md"
    if not path.exists():
        raise KeyError(f"no exportable skill {name!r} — available: {list_exportable()}")
    return path.read_text(encoding="utf-8")


def write_exportable(name: str, dest_dir: Path) -> Path:
    """Write ``<dest_dir>/<name>/SKILL.md``; returns the written path."""
    body = read_exportable(name)  # KeyError didático antes de tocar o disco
    out = Path(dest_dir) / name / "SKILL.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return out
