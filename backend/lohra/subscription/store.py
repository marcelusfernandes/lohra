"""Lohra's own subscription opt-in config: ~/.lohra/auth.json (Fase 10, B1).

Separate from the Codex login — this records whether the USER turned subscription
mode on and acknowledged the ToS risk. Default is OFF. Written chmod 600. The file
holds NO token (the token lives in Codex's auth.json); it's pure opt-in state.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from lohra.safeio import read_text_bounded

_MAX_BYTES = 64_000


@dataclass(frozen=True)
class SubscriptionConfig:
    auth_mode: str  # "subscription" | "api_key"
    acknowledged_tos_risk: bool
    provider: str = "openai"  # OpenAI only in Fase 10

    @property
    def active(self) -> bool:
        """Subscription mode is usable only when on AND the ToS risk is acknowledged."""
        return self.auth_mode == "subscription" and self.acknowledged_tos_risk


def auth_path(home: Path) -> Path:
    return home / "auth.json"


def read_config(home: Path) -> SubscriptionConfig | None:
    """The OpenAI subscription opt-in record, or None if unset/unreadable."""
    text = read_text_bounded(auth_path(home), _MAX_BYTES)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    entry = data.get("openai") if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return None
    return SubscriptionConfig(
        auth_mode=str(entry.get("auth_mode", "api_key")),
        # Fail CLOSED: only a real JSON `true` acknowledges (the string "false",
        # 0, [] etc. must NOT activate a ToS-risky mode).
        acknowledged_tos_risk=entry.get("acknowledged_tos_risk") is True,
    )


def write_config(home: Path, config: SubscriptionConfig) -> None:
    """Persist the opt-in record (chmod 600). Merges into any existing auth.json."""
    path = auth_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    text = read_text_bounded(path, _MAX_BYTES)
    if text:
        try:
            loaded = json.loads(text)
            if isinstance(loaded, dict):
                existing = loaded
        except (ValueError, TypeError):
            existing = {}
    existing["openai"] = {
        "auth_mode": config.auth_mode,
        "acknowledged_tos_risk": config.acknowledged_tos_risk,
    }
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # best-effort on platforms without chmod
