"""`lohra auth` logic — enable/disable/status for subscription mode (Fase 10, B3).

Pure functions so the CLI shell stays thin and the behaviour is testable. Enabling
prints a ToS warning and writes the acknowledgement (the hard gate); disabling
reverts to API key. status never prints a token.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path


import time

from lohra.subscription import store, token_store
from lohra.subscription.codex_creds import read_codex_tokens
from lohra.subscription.credentials import subscription_active
from lohra.subscription.refresh import is_expired

TOS_WARNING = (
    "⚠️  Subscription mode uses your ChatGPT/Codex subscription via your existing "
    "Codex CLI login.\n"
    "    This very likely VIOLATES OpenAI's consumer Terms of Service and may get "
    "your account BANNED.\n"
    "    The endpoints are reverse-engineered and can break without notice. Use at "
    "your own risk.\n"
    "    Lohra reads (never writes) ~/.codex/auth.json; on an expired token it asks "
    "you to refresh via Codex."
)


def status(home: Path) -> dict:
    """A token-free snapshot of subscription state."""
    config = store.read_config(home)
    tokens = read_codex_tokens()
    own = token_store.read_tokens(home)
    return {
        "mode": config.auth_mode if config else "api_key",
        "active": subscription_active(home),
        # Which route the user asked for when both are available (see set_preference).
        "preference": config.preference if config else "auto",
        "acknowledged_tos_risk": bool(config and config.acknowledged_tos_risk),
        # Lohra's own login (auto-refreshing) takes precedence over Codex reuse.
        "own_login": own is not None,
        "own_login_expired": (own.expires_at <= time.time()) if own else None,
        "codex_login_found": tokens is not None,
        "codex_token_expired": is_expired(tokens.access_token) if tokens else None,
        "account_id": (own.account_id if own else None) or (tokens.account_id if tokens else None),
    }


def set_preference(home: Path, value: str) -> None:
    """Record which auth route to take when both are available.

    Deliberately NOT `disable`: preference="api_key" keeps
    ``acknowledged_tos_risk`` on file, so going back to the subscription is one
    command (`lohra auth prefer subscription|auto`) instead of accepting the ToS
    risk again. Every other field of the config is preserved.
    """
    if value not in store.PREFERENCES:  # callers validate; this is the last guard
        raise ValueError(f"unknown auth preference {value!r}")
    config = store.read_config(home) or store.SubscriptionConfig(
        auth_mode="api_key", acknowledged_tos_risk=False
    )
    store.write_config(home, replace(config, preference=value))


def _switch(home: Path, *, auth_mode: str, acknowledged: bool, stale: str) -> None:
    """Flip the mode, clearing ONLY the preference this switch contradicts.

    A mode switch is coarser than the preference, so it must clear the override
    that would make it a lie — but no more than that. `enable` drops a stale
    "api_key" (which would silently make it a no-op); `disable` drops a stale
    "subscription" (which would leave chat erroring out). The other direction is
    deliberately PRESERVED: `lohra auth enable`/`login` are exactly what the
    preference="subscription" error tells the user to run, and wiping their
    choice there would re-arm the silent fallback onto a paid key that
    preference="subscription" exists to prevent. Every other field survives.
    """
    config = store.read_config(home) or store.SubscriptionConfig(
        auth_mode="api_key", acknowledged_tos_risk=False
    )
    preference = "auto" if config.preference == stale else config.preference
    store.write_config(
        home,
        replace(
            config,
            auth_mode=auth_mode,
            acknowledged_tos_risk=acknowledged,
            preference=preference,
        ),
    )


def enable(home: Path) -> None:
    """Turn subscription mode on (records the ToS acknowledgement)."""
    _switch(home, auth_mode="subscription", acknowledged=True, stale="api_key")


def disable(home: Path) -> None:
    """Revert to API-key auth."""
    _switch(home, auth_mode="api_key", acknowledged=False, stale="subscription")
