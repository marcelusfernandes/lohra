"""ProviderProfile — a declarative description of an LLM provider.

A profile DESCRIBES a provider; it does not own client construction, credential
rotation, or streaming (those stay on the agent). Profiles are registered into a
process-wide registry indexed by name and aliases (last-writer-wins).

See docs/specs/01-agent-core.md §3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Sentinel: provider must NOT receive a temperature parameter at all.


@dataclass(frozen=True)
class ProviderProfile:
    """Declarative provider metadata. Override the hooks for provider quirks."""

    name: str
    api_mode: str = "chat_completions"
    aliases: tuple[str, ...] = ()
    display_name: str = ""
    description: str = ""
    signup_url: str = ""
    env_vars: tuple[str, ...] = ()
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"  # api_key | oauth_device_code | oauth_external | aws_sdk
    requires_api_key: bool = True  # False for keyless local endpoints (e.g. ollama)
    supports_vision: bool = False
    fallback_models: tuple[str, ...] = ()
    hostname: str = ""
    default_headers: dict[str, str] = field(default_factory=dict)
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    # Janela de contexto (INPUT) assumida pela compactação preflight. None = o
    # perfil não faz claim algum (endpoint local, modelo desconhecido) e quem
    # resolve cai no fallback. Ver ``get_context_window``.
    default_context_window: int | None = None
    # Janela por MODELO, sobrepondo ``default_context_window``. Um provider serve
    # modelos de janelas muito diferentes (a anthropic tem 200k E 1M sob o mesmo
    # perfil); o piso único não distingue. Chave = PREFIXO do slug do modelo, e
    # ``get_context_window`` casa o prefixo MAIS LONGO — assim ``claude-opus-4-5``
    # (200k) não é confundido com ``claude-opus-4-6`` (1M) e sufixos de data são
    # absorvidos. Mesmo padrão mutável-em-frozen de ``default_headers``.
    model_windows: Mapping[str, int] = field(default_factory=dict)
    default_aux_model: str = ""

    # --- Overridable hooks (default = pass-through) ---

    def get_hostname(self) -> str:
        """Hostname for URL->provider reverse mapping; derived from base_url."""
        if self.hostname:
            return self.hostname
        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname or ""

    def get_max_tokens(self, model: str) -> int | None:
        """Per-model max-tokens cap; falls back to ``default_max_tokens``."""
        return self.default_max_tokens

    def get_context_window(self, model: str) -> int | None:
        """Per-model context window; falls back to ``default_context_window``.

        Casa o PREFIXO mais longo de ``model_windows`` que seja prefixo de
        ``model`` (nunca substring solto: só ``model.startswith(key)``). Chave
        vazia jamais casa. Sem match → ``default_context_window``, que pode ser
        ``None``: "não sei" é resposta legítima (ollama serve o que o operador
        puxou) e quem chama decide o fallback.
        """
        best_key = ""
        for key in self.model_windows:
            if key and len(key) > len(best_key) and model.startswith(key):
                best_key = key
        if best_key:
            return self.model_windows[best_key]
        return self.default_context_window


# --- Process-wide registry (resolution: arg -> config -> env -> "auto") ---

_REGISTRY: dict[str, ProviderProfile] = {}
_ALIASES: dict[str, str] = {}


def register_provider(profile: ProviderProfile) -> None:
    """Register a profile by name and every alias (last-writer-wins)."""
    _REGISTRY[profile.name] = profile
    for alias in profile.aliases:
        _ALIASES[alias] = profile.name


def get_provider_profile(name: str) -> ProviderProfile | None:
    """Resolve a profile by name or alias. Returns None for unknown providers."""
    key = name.lower()
    if key in _REGISTRY:
        return _REGISTRY[key]
    if key in _ALIASES:
        return _REGISTRY.get(_ALIASES[key])
    return None


def list_providers() -> list[ProviderProfile]:
    """All registered profiles."""
    return list(_REGISTRY.values())
