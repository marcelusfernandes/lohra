"""Read an existing Codex CLI login from ~/.codex/auth.json (Fase 10, B1).

Lohra REUSES the Codex login rather than re-implementing OAuth. The file is read
safely (bounded, symlink-rejected) and its absence is NOT an error — Codex may
store creds in the OS keyring (out of scope) or the user may not have logged in.
Tokens are never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from lohra.safeio import read_text_bounded

_MAX_AUTH_BYTES = 256_000  # auth.json is tiny; bound it anyway (untrusted-shape input)


@dataclass(frozen=True)
class CodexTokens:
    access_token: str
    refresh_token: str
    account_id: str | None

    def __repr__(self) -> str:  # never render tokens (repr leaks into tracebacks/logs)
        return f"CodexTokens(access_token=***, refresh_token=***, account_id={self.account_id!r})"


def _codex_home() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home) if home else Path.home() / ".codex"


def codex_auth_path() -> Path:
    """$CODEX_HOME/auth.json, else ~/.codex/auth.json (Codex's documented default)."""
    return _codex_home() / "auth.json"


def read_codex_model() -> str | None:
    """The `model` from the user's Codex config.toml, so subscription mode defaults
    to whatever their Codex is set to (slugs vary by account/version). None if unset."""
    text = read_text_bounded(_codex_home() / "config.toml", _MAX_AUTH_BYTES)
    if text is None:
        return None
    try:
        import tomllib

        data = tomllib.loads(text)
    except (ValueError, ModuleNotFoundError):
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model else None


def read_codex_tokens(path: Path | None = None) -> CodexTokens | None:
    """The Codex access/refresh tokens + account id, or None if not available
    (file missing/keyring/unreadable/malformed/no access_token). Never raises."""
    text = read_text_bounded(path or codex_auth_path(), _MAX_AUTH_BYTES)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = tokens.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    refresh = tokens.get("refresh_token")
    account = tokens.get("account_id")
    return CodexTokens(
        access_token=access,
        refresh_token=refresh if isinstance(refresh, str) else "",
        account_id=account if isinstance(account, str) and account else None,
    )
