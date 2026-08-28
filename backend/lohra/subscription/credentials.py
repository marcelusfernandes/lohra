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


_PREFER_KEY_NOTE = (
    "note: your OpenAI/Codex subscription is active, but preference=api_key — "
    "using your API key (`lohra auth prefer auto` to go back)."
)

_PREFER_SUB_ERROR = (
    "preference=subscription, but subscription mode is not usable: run "
    "`lohra auth enable` to opt in (accepts the ToS risk) and `lohra auth login` "
    "to log in (or reuse `codex login`). To fall back to an API key instead, run "
    "`lohra auth prefer auto`."
)


@dataclass(frozen=True)
class AuthRoute:
    """Which auth path this invocation takes. Check ``error`` FIRST: when it is
    set the caller must abort (exit 2) — ``mode`` is then meaningless."""

    mode: str  # "subscription" | "api_key"
    note: str | None = None  # one stderr line explaining a non-obvious choice
    error: str | None = None  # didactic, token-free; abort when set


def resolve_auth_route(home: Path) -> AuthRoute:
    """The single decision point for subscription-vs-API-key (chat AND dashboard).

    Truth table (``preference`` lives in auth.json, per profile):

    * ``auto`` (the default, and what an absent/garbage value reads as) —
      exactly today's ``if subscription_active(home)``.
    * ``api_key`` — the API-key path even when the subscription is usable, with
      one note on stderr. Unlike ``lohra auth disable`` this KEEPS the ToS
      acknowledgement, so coming back is one command, not a re-acceptance.
    * ``subscription`` — fails loudly when the subscription is unusable; a
      silent fall back to a billed API key would ignore an explicit choice.

    The ``lohra serve`` refusal does NOT go through here: it is an unconditional
    security gate (relaying the subscription would expose it), not a preference.
    The cross-provider escalation gate DOES, on top of its own opt-in check — a
    workflow node's ``provider: "openai-codex"`` is agent-authored, so it must
    not outrank the human's stored choice on a billed, ToS-gray path.
    """
    config = store.read_config(home)
    return route_for(
        config.preference if config is not None else "auto",
        config is not None and config.active,
    )


def route_for(preference: str, active: bool) -> AuthRoute:
    """The truth table itself, as a pure function of (preference, opt-in state).

    Split out from ``resolve_auth_route`` so the consumers that already hold
    those two facts — the ``detect`` snapshot behind ``lohra doctor``/``init`` —
    answer "which path will chat take?" with THIS table instead of a second copy
    of it. One table, so a doctor line can never disagree with chat.
    """
    if preference == "api_key":
        return AuthRoute(mode="api_key", note=_PREFER_KEY_NOTE if active else None)
    if preference == "subscription" and not active:
        return AuthRoute(mode="api_key", error=_PREFER_SUB_ERROR)
    return AuthRoute(mode="subscription" if active else "api_key")


def routes_to_subscription(home: Path) -> bool:
    """Will this store actually RIDE the subscription? (opt-in AND preference.)

    ``subscription_active`` answers a different question — "is the opt-in on
    file?" — and stays the right one for the security gates.
    """
    return resolve_auth_route(home).mode == "subscription"


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
