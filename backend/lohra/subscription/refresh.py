"""Access-token expiry check + refresh (Fase 10, B1).

The Codex access_token is a short-lived JWT; Lohra refreshes it itself (the SDK
won't refresh a static key). The HTTP POST is injectable so the logic is testable
offline. Tokens never appear in log lines or exception messages.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from lohra.subscription.constants import CODEX_CLIENT_ID, REFRESH_URL

# Inject a poster (url, json_body) -> response dict; default uses httpx.
HttpPost = Callable[[str, dict], dict]

_EXPIRY_SKEW_SECONDS = 300  # treat as expired 5 min early (matches Codex's window)


class RefreshError(Exception):
    """Token refresh failed (network/4xx/malformed) — never carries the token."""


@dataclass(frozen=True)
class RefreshResult:
    """A fresh access token + the ROTATED refresh token (the server rotates it; a
    caller persisting these must write BOTH back, else the next refresh fails)."""

    access_token: str
    refresh_token: str  # may be "" if the server didn't rotate

    def __repr__(self) -> str:  # never render the secrets
        return "RefreshResult(access_token=***, refresh_token=***)"


def _jwt_exp(token: str) -> int | None:
    """The `exp` (unix seconds) from a JWT payload, or None if unparseable.
    No signature verification — we only read the expiry to decide on refresh."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    exp = data.get("exp") if isinstance(data, dict) else None
    return exp if isinstance(exp, int) else None


def is_expired(token: str, *, now: float | None = None) -> bool:
    """Whether the JWT is expired (or expires within the skew). Unparseable →
    treated as expired so the caller refreshes rather than sends a dead token."""
    exp = _jwt_exp(token)
    if exp is None:
        return True
    return (now if now is not None else time.time()) >= exp - _EXPIRY_SKEW_SECONDS


def refresh_access_token(refresh_token: str, post: HttpPost) -> RefreshResult:
    """Exchange a refresh_token for a fresh access token + the ROTATED refresh
    token via the public OAuth endpoint. Raises RefreshError on any failure
    (message is token-free). The caller MUST persist both rotated values (B4) —
    the old refresh token is single-use and dead after this call."""
    if not refresh_token:
        raise RefreshError("no refresh token available")
    try:
        body = post(
            REFRESH_URL,
            {
                "client_id": CODEX_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
    except Exception as exc:  # network etc. — never echo the request body
        raise RefreshError(f"refresh request failed: {type(exc).__name__}") from None
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise RefreshError("refresh response had no access_token")
    rotated = body.get("refresh_token")  # server rotates it; keep it for write-back
    return RefreshResult(
        access_token=access,
        refresh_token=rotated if isinstance(rotated, str) else "",
    )


def default_post(url: str, json_body: dict) -> dict[str, Any]:
    """The real HTTP poster (httpx). Imported lazily so tests need no network."""
    import httpx

    resp = httpx.post(url, json=json_body, timeout=30.0)
    resp.raise_for_status()
    return resp.json()
