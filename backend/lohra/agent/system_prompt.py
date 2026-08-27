"""3-tier system prompt — Invariante #1 (spec §7).

Built ONCE per session and frozen; only recompiled after context compression.
Tiers are ordered most-stable -> least-stable so the provider's prefix cache
stays warm across turns:

- stable: identity + guidance + environment hints. Byte-stable per process.
- context: caller system_message + context files (AGENTS.md, .cursorrules).
- volatile: memory snapshot, user profile, DATE-ONLY timestamp (clock time
  would invalidate the cache on every request).

Memory and skills update the disk, never a live snapshot.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from functools import cached_property
from typing import Mapping

DEFAULT_IDENTITY = (
    "You are Lohra, a self-improving AI assistant. You are helpful, "
    "knowledgeable, and direct. You use tools to take real action and you "
    "never fabricate results — reporting a blocker honestly is always better "
    "than inventing an outcome."
)

TIER_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class SystemPromptSnapshot:
    """Immutable per-session system prompt; the loop reads only ``text``."""

    stable: str
    context: str
    volatile: str

    @cached_property
    def text(self) -> str:
        tiers = (self.stable, self.context, self.volatile)
        return TIER_SEPARATOR.join(tier for tier in tiers if tier)


def _render_environment_hints(hints: Mapping[str, str]) -> str:
    if not hints:
        return ""
    lines = "\n".join(f"- {key}: {hints[key]}" for key in sorted(hints))
    return f"Environment:\n{lines}"


def _render_context_files(files: tuple[tuple[str, str], ...]) -> str:
    return TIER_SEPARATOR.join(
        f'<context-file name="{name}">\n{content}\n</context-file>'
        for name, content in files
        if content
    )


def build_system_prompt(
    *,
    identity: str | None = None,
    environment_hints: Mapping[str, str] | None = None,
    system_message: str | None = None,
    context_files: tuple[tuple[str, str], ...] = (),
    memory_snapshot: str = "",
    user_profile: str = "",
    skills_index: str = "",
    today: datetime.date | None = None,
) -> SystemPromptSnapshot:
    """Assemble the frozen snapshot. ``today`` is injectable for tests.

    ``identity`` defaults to the built-in identity; pass SOUL.md content to
    override it (stable-tier slot #1, spec §3).
    """
    stable_parts = [identity or DEFAULT_IDENTITY, _render_environment_hints(environment_hints or {})]

    context_parts = [
        (system_message or "").strip(),
        _render_context_files(context_files),
    ]

    date_text = (today or datetime.date.today()).isoformat()
    volatile_parts = [
        f"<memory>\n{memory_snapshot}\n</memory>" if memory_snapshot else "",
        f"<user-profile>\n{user_profile}\n</user-profile>" if user_profile else "",
        skills_index,
        f"Today's date is {date_text}.",
    ]

    def _join(parts: list[str]) -> str:
        return TIER_SEPARATOR.join(part for part in parts if part)

    return SystemPromptSnapshot(
        stable=_join(stable_parts),
        context=_join(context_parts),
        volatile=_join(volatile_parts),
    )
