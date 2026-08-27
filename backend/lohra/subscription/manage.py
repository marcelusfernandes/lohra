"""`lohra auth` logic — enable/disable/status for subscription mode (Fase 10, B3).

Pure functions so the CLI shell stays thin and the behaviour is testable. Enabling
prints a ToS warning and writes the acknowledgement (the hard gate); disabling
reverts to API key. status never prints a token.
"""

from __future__ import annotations

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
        "acknowledged_tos_risk": bool(config and config.acknowledged_tos_risk),
        # Lohra's own login (auto-refreshing) takes precedence over Codex reuse.
        "own_login": own is not None,
        "own_login_expired": (own.expires_at <= time.time()) if own else None,
        "codex_login_found": tokens is not None,
        "codex_token_expired": is_expired(tokens.access_token) if tokens else None,
        "account_id": (own.account_id if own else None) or (tokens.account_id if tokens else None),
    }


def enable(home: Path) -> None:
    """Turn subscription mode on (records the ToS acknowledgement)."""
    store.write_config(
        home, store.SubscriptionConfig(auth_mode="subscription", acknowledged_tos_risk=True)
    )


def disable(home: Path) -> None:
    """Revert to API-key auth."""
    store.write_config(
        home, store.SubscriptionConfig(auth_mode="api_key", acknowledged_tos_risk=False)
    )
