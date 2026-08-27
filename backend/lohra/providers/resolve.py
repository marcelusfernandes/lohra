"""Provider resolution — arg -> config -> env -> "auto" (spec §3).

The env step has two tiers: the explicit ``LOHRA_PROVIDER`` variable wins over
API-key detection, which scans registered profiles' ``env_vars`` in
registration order.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from lohra.providers.base import get_provider_profile, list_providers

ENV_PROVIDER_VAR = "LOHRA_PROVIDER"
AUTO_PROVIDER = "auto"


def _canonicalize(name: str) -> str:
    """Resolve a name or alias to the canonical profile name; fail fast on typos."""
    profile = get_provider_profile(name)
    if profile is None:
        known = ", ".join(sorted(p.name for p in list_providers()))
        raise ValueError(f"unknown provider {name!r} (known: {known})")
    return profile.name


def resolve_provider_name(
    arg: str | None = None,
    config_value: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Pick the provider name: arg -> config -> env -> "auto".

    Explicit choices (arg, config, LOHRA_PROVIDER) are validated against the
    registry and canonicalized. API-key detection only ever yields registered
    names; when several API keys are set, registration order decides (builtins
    register anthropic first). Empty/whitespace values count as unset at every
    level. The "auto" fallback is NOT a registry name — callers must treat it
    as a sentinel, never feed it back into a registry lookup.
    """
    environ = os.environ if env is None else env

    arg = (arg or "").strip()
    config_value = (config_value or "").strip()
    explicit_env = (environ.get(ENV_PROVIDER_VAR) or "").strip()

    if arg:
        return _canonicalize(arg)
    if config_value:
        return _canonicalize(config_value)
    if explicit_env:
        return _canonicalize(explicit_env)

    for profile in list_providers():
        if any(environ.get(var) for var in profile.env_vars):
            return profile.name

    return AUTO_PROVIDER
