"""Model tiers (WF-5) — ``small``/``medium``/``big`` instead of a hard slug.

A spec that names ``model: claude-opus-4-8`` stops being portable the moment it
becomes a template: the slug may not exist on another profile, another provider,
or another machine — and a template that only runs where it was authored is not
a template. A node names a TIER instead, and the OPERATOR maps that tier to a
real model.

The map lives in operator config (``~/.lohra/workflow_tiers.json``), NEVER in the
spec — exactly the rule the capability policy already follows: an authored (or
injected) spec must not be able to point itself at a model the operator did not
sanction.

Shape (every field optional except that a tier must say SOMETHING)::

    {"big":    {"model": "claude-opus-4-8", "effort": "high"},
     "medium": {"model": "claude-sonnet-4-6"},
     "small":  "claude-haiku-4-5"}

The tier set is CLOSED: an unknown key is dropped rather than becoming a fourth
tier no spec can name and no validator would accept.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODEL_TIERS = ("small", "medium", "big")


@dataclass(frozen=True)
class Tier:
    """What one tier resolves to. Any subset may be set; None means "unchanged"."""

    model: str | None = None
    provider: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class TierMap:
    """The operator's tier -> configuration map (closed set, copied on build)."""

    tiers: dict[str, Tier] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kept = {
            name: value
            for name, value in self.tiers.items()
            if name in MODEL_TIERS and isinstance(value, Tier)
        }
        object.__setattr__(self, "tiers", kept)

    def get(self, name: Any) -> Tier | None:
        """The tier named, or None — including for a name outside the closed set."""
        return self.tiers.get(name) if isinstance(name, str) else None


def _text(value: Any) -> str | None:
    """A non-empty string, else None (a typed-wrong entry is simply not set)."""
    return value if isinstance(value, str) and value.strip() else None


def _as_tier(entry: Any) -> Tier | None:
    """Normalise one authored tier entry, or None to DROP it.

    A bare string is the model shorthand (the common case). A tier that resolves
    to nothing at all is dropped rather than kept as a no-op that would silently
    swallow the "no mapping" warning."""
    if isinstance(entry, Tier):
        return entry
    if isinstance(entry, str):
        model = _text(entry)
        return Tier(model=model) if model else None
    if not isinstance(entry, dict):
        return None
    tier = Tier(
        model=_text(entry.get("model")),
        provider=_text(entry.get("provider")),
        effort=_text(entry.get("effort")),
    )
    return tier if (tier.model or tier.provider or tier.effort) else None


def load_tiers(path: Path) -> TierMap:
    """Load ``~/.lohra/workflow_tiers.json``; empty (every tier unmapped) if the
    file is absent or unreadable. An unmapped tier is a WARNING at run time, not
    a failure — the node runs on the run's default model."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return TierMap()
    if not isinstance(data, dict):
        return TierMap()
    resolved: dict[str, Tier] = {}
    for name in MODEL_TIERS:
        tier = _as_tier(data.get(name))
        if tier is not None:
            resolved[name] = tier
    return TierMap(resolved)
