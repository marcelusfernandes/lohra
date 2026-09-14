"""Lohra's OWN OAuth token store: ~/.lohra/oauth.json (Fase 10, B5).

When the user runs `lohra auth login`, Lohra mints its OWN token family (separate
from the Codex CLI's) and stores it here. Because Lohra owns this token, it can
refresh + persist the rotated refresh token SAFELY — no race with Codex's own
writes (the reason B1's reuse path couldn't refresh). chmod 600, never logged.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from lohra.safeio import read_text_bounded
from lohra.subscription.errors import SubscriptionError
from lohra.subscription.persistence import atomic_write, profile_transaction

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


def read_tokens(home: Path, *, strict: bool = False) -> OAuthTokens | None:
    """Read a snapshot; strict request reads refuse a present, unusable own login.

    Status consumers retain the historical None-on-invalid contract. A request
    must never turn a broken own store into a different Codex account silently.
    """
    path = token_path(home)
    text = read_text_bounded(path, _MAX_BYTES)
    try:
        if text is None:
            path.lstat()  # absent is the only condition permitting Codex reuse
            raise ValueError
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError
        expiry = data.get("expires_at", 0)
        if isinstance(expiry, bool):
            raise ValueError
        tokens = OAuthTokens(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token") if isinstance(data.get("refresh_token"), str) else "",
            account_id=data.get("account_id"),
            expires_at=float(expiry),
        )
        validate_tokens(tokens)
        return tokens
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, SubscriptionError):
        if strict:
            raise SubscriptionError("stored login is unreadable or invalid — run `lohra auth login`") from None
        return None


def valid_header(value: object) -> bool:
    """Nonempty printable ASCII only: neither HTTP errors nor tracebacks echo it."""
    return (
        isinstance(value, str) and bool(value) and value == value.strip()
        and all(32 <= ord(c) < 127 for c in value)
    )


def finite_number(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def validate_tokens(tokens: OAuthTokens) -> None:
    if (
        not valid_header(tokens.access_token)
        or (tokens.account_id is not None and not valid_header(tokens.account_id))
        or not isinstance(tokens.refresh_token, str)
        or not finite_number(tokens.expires_at)
    ):
        raise SubscriptionError("login response is invalid — run `lohra auth login` again")


def write_tokens(home: Path, tokens: OAuthTokens) -> None:
    """Persist the token family (chmod 600). Used after login AND after a refresh
    (to keep the rotated refresh token — the old one is single-use)."""
    with profile_transaction(home) as home:
        _write_tokens_locked(home, tokens)


def _write_tokens_locked(home: Path, tokens: OAuthTokens) -> None:
    """Caller holds the profile transaction, including refresh's read + HTTP."""
    validate_tokens(tokens)
    path = token_path(home)
    data = json.dumps(
        {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "account_id": tokens.account_id,
            "expires_at": tokens.expires_at,
        },
        indent=2,
    )
    if len(data.encode("utf-8")) > _MAX_BYTES:
        raise SubscriptionError("login response exceeds the store limit — run `lohra auth login`")
    atomic_write(path, data)


def clear_tokens(home: Path) -> bool:
    """Remove the stored tokens (logout). True if a file was removed."""
    with profile_transaction(home) as home:
        try:
            token_path(home).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            raise SubscriptionError("could not remove the login — check profile permissions") from None
