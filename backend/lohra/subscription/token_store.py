"""Lohra's OWN OAuth token store: ~/.lohra/oauth.json (Fase 10, B5).

When the user runs `lohra auth login`, Lohra mints its OWN token family (separate
from the Codex CLI's) and stores it here. Because Lohra owns this token, it can
refresh + persist the rotated refresh token SAFELY — no race with Codex's own
writes (the reason B1's reuse path couldn't refresh). chmod 600, never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from lohra.safeio import read_text_bounded

_MAX_BYTES = 64_000


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str
    account_id: str | None
    expires_at: float  # unix seconds

    def __repr__(self) -> str:  # never render the tokens
        return f"OAuthTokens(access_token=***, refresh_token=***, account_id={self.account_id!r})"


def token_path(home: Path) -> Path:
    return home / "oauth.json"


def read_tokens(home: Path) -> OAuthTokens | None:
    """Lohra's stored OAuth tokens, or None if not logged in / unreadable."""
    text = read_text_bounded(token_path(home), _MAX_BYTES)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    return OAuthTokens(
        access_token=access,
        refresh_token=data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else "",
        account_id=data.get("account_id") if isinstance(data.get("account_id"), str) else None,
        expires_at=float(data.get("expires_at") or 0),
    )


def write_tokens(home: Path, tokens: OAuthTokens) -> None:
    """Persist the token family (chmod 600). Used after login AND after a refresh
    (to keep the rotated refresh token — the old one is single-use)."""
    path = token_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "account_id": tokens.account_id,
            "expires_at": tokens.expires_at,
        },
        indent=2,
    )
    # Write the temp file at 0600 FROM CREATION (no umask window), then atomically
    # rename — a refresh token must never briefly exist at world-readable perms.
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def clear_tokens(home: Path) -> bool:
    """Remove the stored tokens (logout). True if a file was removed."""
    path = token_path(home)
    try:
        path.unlink()
        return True
    except OSError:
        return False
