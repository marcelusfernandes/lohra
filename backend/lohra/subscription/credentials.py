"""Resolve effective subscription credentials + enforce the opt-in gate (Fase 10, B1).

Ties the pieces together: the opt-in config (store), the Codex login (codex_creds),
and refresh. Returns everything the Responses client (B2) needs — token, account
id, base_url, headers — or None when subscription mode is off. Raises a clear,
token-free error when it's on but unusable, so the caller can fall back to api_key.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from lohra.subscription import oauth, store, token_store
from lohra.subscription.codex_creds import read_codex_tokens
from lohra.subscription.constants import (
    ACCOUNT_ID_HEADER,
    CODEX_BASE_URL,
    ORIGINATOR,
    ORIGINATOR_HEADER,
)
from lohra.subscription.refresh import is_expired


class SubscriptionError(Exception):
    """Subscription mode is on but unusable — message is always token-free."""


@dataclass(frozen=True)
class SubscriptionCreds:
    token: str
    account_id: str | None
    base_url: str
    headers: dict[str, str]

    def __repr__(self) -> str:  # never render the bearer token (repr leaks into tracebacks/logs)
        return f"SubscriptionCreds(token=***, account_id={self.account_id!r}, base_url={self.base_url!r})"


def subscription_active(home: Path) -> bool:
    """Whether the user opted into subscription mode AND acknowledged the ToS risk."""
    config = store.read_config(home)
    return config is not None and config.active


_EXPIRY_SKEW = 300  # refresh our own token 5 min early (matches Codex's window)


def _creds(token: str, account_id: str | None) -> SubscriptionCreds:
    headers = {ORIGINATOR_HEADER: ORIGINATOR}
    if account_id:
        headers[ACCOUNT_ID_HEADER] = account_id
    return SubscriptionCreds(token=token, account_id=account_id, base_url=CODEX_BASE_URL, headers=headers)


def resolve(home: Path, *, now: float | None = None, post: Any | None = None) -> SubscriptionCreds | None:
    """Effective creds, or None if subscription mode is off. Raises a token-free
    SubscriptionError when on-but-unusable.

    Two token sources, in precedence: (1) Lohra's OWN login (`lohra auth login`,
    ~/.lohra/oauth.json) — transparently REFRESHED + persisted here, safe because
    we own the token family; (2) fallback: reuse the Codex CLI login (~/.codex/
    auth.json) WITHOUT refresh (rotating it would race Codex → expired asks the
    user to run `codex`)."""
    config = store.read_config(home)
    if config is None or config.auth_mode != "subscription":
        return None
    if not config.acknowledged_tos_risk:
        raise SubscriptionError(
            "subscription mode is set but the ToS risk is not acknowledged — "
            "run `lohra auth enable` to confirm (default stays API key)"
        )

    own = token_store.read_tokens(home)
    if own is not None:
        clock = now if now is not None else time.time()
        if clock >= own.expires_at - _EXPIRY_SKEW:
            own = _refresh_own(home, own, post)  # transparent refresh + persist
        return _creds(own.access_token, own.account_id)

    tokens = read_codex_tokens()
    if tokens is None:
        raise SubscriptionError(
            "not logged in — run `lohra auth login` (own login, auto-refresh) or "
            "`codex login` (reuse), or unset subscription mode to use an API key"
        )
    if is_expired(tokens.access_token, now=now):
        raise SubscriptionError(
            "the Codex token is expired — run any `codex` command to refresh it, "
            "run `lohra auth login` for a self-refreshing login, or use an API key"
        )
    return _creds(tokens.access_token, tokens.account_id)


def _refresh_own(home: Path, tokens: Any, post: Any | None):
    """Refresh Lohra's own token + persist the rotated family. Token-free errors.

    No cross-process lock: if a concurrent process (e.g. the dashboard) refreshed
    in the same window, our refresh of the now-rotated token fails — so on failure
    we re-read the store and use the fresh token the winner just wrote, before
    surfacing an error. Common single-process use never races."""
    try:
        fresh = oauth.refresh_tokens(tokens.refresh_token, post or oauth.default_post)
    except oauth.OAuthError as exc:
        latest = token_store.read_tokens(home)  # did another process just rotate it?
        if latest is not None and latest.access_token != tokens.access_token:
            return latest
        raise SubscriptionError(
            f"could not refresh the login ({exc}) — run `lohra auth login` again"
        ) from None
    if not fresh.account_id:  # the refresh response may omit it; keep what we had
        fresh = replace(fresh, account_id=tokens.account_id)
    token_store.write_tokens(home, fresh)
    return fresh
