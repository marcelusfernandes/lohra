"""Provider abstraction and plugin registry.

Importing this package registers the built-in profiles (anthropic, openai).
See docs/specs/01-agent-core.md §3.
"""

import lohra.providers.transports  # noqa: F401  — populates the transport registry
from lohra.providers.base import (
    ProviderProfile,
    get_provider_profile,
    list_providers,
    register_provider,
)
from lohra.providers.builtin import register_builtin_providers
from lohra.providers.resolve import resolve_provider_name

register_builtin_providers()

__all__ = [
    "ProviderProfile",
    "get_provider_profile",
    "list_providers",
    "register_provider",
    "resolve_provider_name",
]
