"""Per-session pool of provider clients for CROSS-PROVIDER delegation.

Lets a subagent / workflow leaf / sub-session run on a DIFFERENT provider than the
orchestrator (e.g. Claude orchestrator → GPT or Codex subagent). Builds + caches one
client per target provider (so concurrent spawns don't each open an httpx pool),
under a lock. BORROWS the parent client (never closes it); closes only clients it
built. A target provider with no credential fails just that subagent (clean error),
never a crash. Subscription (openai-codex) is gated — it never auto-escalates.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from lohra.agent.client import build_client, resolve_api_key
from lohra.agent.overrides import make_configure
from lohra.providers import get_provider_profile

# Providers build_client can construct directly (api-key path). "responses" (Codex)
# is reachable only via the subscription builder, gated below.
_BUILDABLE_API_MODES = ("anthropic_messages", "chat_completions")


class ProviderError(Exception):
    """A target provider can't be resolved/built — token-free; fails one subagent."""


class ClientPool:
    """Resolves (ProviderProfile, ModelClient) for a target provider name."""

    def __init__(self, parent_provider: Any, parent_client: Any, home: Path) -> None:
        self._parent_provider = parent_provider
        self._parent_name = parent_provider.name
        self._parent_client = parent_client  # BORROWED — never closed here
        self._home = home
        self._owned: dict[str, tuple[Any, Any]] = {}
        self._lock = threading.Lock()

    def get(self, name: str | None) -> tuple[Any, Any]:
        """(profile, client) for ``name``; the parent pair for the parent/empty name.
        Builds + caches under a lock. Raises ProviderError on any failure."""
        if not name or name == self._parent_name:
            return self._parent_provider, self._parent_client
        with self._lock:
            if name not in self._owned:
                self._owned[name] = self._build(name)
            return self._owned[name]

    def _build(self, name: str) -> tuple[Any, Any]:
        if name == "openai-codex":
            return self._build_subscription()
        profile = get_provider_profile(name)
        if profile is None:
            raise ProviderError(f"unknown provider {name!r}")
        if profile.api_mode not in _BUILDABLE_API_MODES:
            raise ProviderError(f"provider {name!r} (api_mode {profile.api_mode!r}) is not supported")
        if profile.requires_api_key and resolve_api_key(profile) is None:
            raise ProviderError(f"no API key configured for provider {name!r}")
        try:
            return profile, build_client(profile)
        except Exception as exc:  # token-free: never echo construction internals
            raise ProviderError(f"could not build a client for {name!r}: {type(exc).__name__}") from None

    def _build_subscription(self) -> tuple[Any, Any]:
        # Hard gate: a child must NOT silently escalate a plain-key parent onto the
        # ToS-gray subscription just because it requested provider="openai-codex".
        # Workflow specs are agent-authored, so that request is not the human's
        # voice — the stored preference is, and it wins on a billed, ToS-gray path.
        from lohra.subscription.credentials import (
            SubscriptionError,
            resolve_auth_route,
            subscription_active,
        )
        from lohra.subscription.provider import CODEX_PROVIDER, build_subscription_client

        if not subscription_active(self._home):
            raise ProviderError(
                "subscription not enabled — run `lohra auth enable` (won't auto-escalate)"
            )
        if resolve_auth_route(self._home).mode != "subscription":
            raise ProviderError(
                "subscription opted in but not preferred — run `lohra auth prefer auto` "
                "to let a child use it (won't override your choice)"
            )
        try:
            return CODEX_PROVIDER, build_subscription_client(self._home)
        except SubscriptionError as exc:
            raise ProviderError(f"subscription: {exc}") from None

    def close(self) -> None:
        """Close ONLY the clients this pool built (the parent client is borrowed)."""
        with self._lock:
            for _profile, client in self._owned.values():
                try:
                    client.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._owned.clear()


def configure_for(
    pool: ClientPool | None,
    *,
    provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    forced_tool: dict | None = None,
    max_iterations: int | None = None,
) -> Callable[[Any], None] | None:
    """Build a configure hook, resolving a cross-provider override via the pool.
    Raises ProviderError if a provider override can't be resolved. None override →
    None hook (byte-identical default)."""
    profile = client = None
    if provider:
        if pool is None:
            raise ProviderError("cross-provider delegation is not available here")
        profile, client = pool.get(provider)
        if not model:  # the parent's slug is meaningless on another provider
            model = profile.fallback_models[0] if profile.fallback_models else None
            if not model:  # no default → don't leave the parent's foreign slug (would 400)
                raise ProviderError(f"provider {provider!r} has no default model — pass an explicit model")
    return make_configure(
        model=model,
        effort=effort,
        forced_tool=forced_tool,
        provider=profile,
        client=client,
        max_iterations=max_iterations,
    )
