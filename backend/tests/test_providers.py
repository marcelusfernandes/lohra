"""Tests for built-in provider profiles and provider resolution.

Phase 1 — see docs/specs/01-agent-core.md §3. Resolution order is
arg -> config -> env -> "auto".
"""

import pytest

from lohra.providers import get_provider_profile
from lohra.providers.resolve import resolve_provider_name
from lohra.providers.transports import get_transport


# --- built-in profiles ---


def test_anthropic_profile_registered():
    profile = get_provider_profile("anthropic")
    assert profile is not None
    assert profile.api_mode == "anthropic_messages"
    assert "ANTHROPIC_API_KEY" in profile.env_vars
    assert profile.get_hostname() == "api.anthropic.com"
    assert profile.supports_vision


def test_anthropic_profile_resolvable_by_alias():
    assert get_provider_profile("claude").name == "anthropic"


def test_anthropic_profile_api_mode_has_registered_transport():
    profile = get_provider_profile("anthropic")
    assert get_transport(profile.api_mode) is not None


def test_anthropic_profile_default_models():
    profile = get_provider_profile("anthropic")
    assert profile.fallback_models[0] == "claude-opus-4-8"
    assert profile.default_aux_model == "claude-haiku-4-5"


def test_openai_profile_registered():
    profile = get_provider_profile("openai")
    assert profile is not None
    assert profile.api_mode == "chat_completions"
    assert "OPENAI_API_KEY" in profile.env_vars
    assert profile.get_hostname() == "api.openai.com"


# --- resolution: arg -> config -> env -> "auto" ---


def test_resolve_arg_wins_over_everything():
    name = resolve_provider_name(
        arg="openai",
        config_value="anthropic",
        env={"LOHRA_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
    )
    assert name == "openai"


def test_resolve_arg_canonicalizes_alias():
    assert resolve_provider_name(arg="claude", env={}) == "anthropic"


def test_resolve_unknown_arg_raises():
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_provider_name(arg="not-a-provider", env={})


def test_resolve_config_wins_over_env():
    name = resolve_provider_name(
        config_value="openai",
        env={"LOHRA_PROVIDER": "anthropic"},
    )
    assert name == "openai"


def test_resolve_explicit_env_var():
    assert resolve_provider_name(env={"LOHRA_PROVIDER": "anthropic"}) == "anthropic"


def test_resolve_detects_provider_from_api_key_env():
    assert resolve_provider_name(env={"ANTHROPIC_API_KEY": "k"}) == "anthropic"
    assert resolve_provider_name(env={"OPENAI_API_KEY": "k"}) == "openai"


def test_resolve_explicit_env_wins_over_key_detection():
    name = resolve_provider_name(
        env={"LOHRA_PROVIDER": "openai", "ANTHROPIC_API_KEY": "k"}
    )
    assert name == "openai"


def test_resolve_nothing_set_falls_back_to_auto():
    assert resolve_provider_name(env={}) == "auto"


def test_resolve_empty_strings_are_ignored():
    name = resolve_provider_name(arg="", config_value="", env={"LOHRA_PROVIDER": ""})
    assert name == "auto"


def test_resolve_whitespace_values_are_ignored():
    name = resolve_provider_name(arg="  ", config_value=" ", env={"LOHRA_PROVIDER": "\t"})
    assert name == "auto"


# --- os 3 diretos pedidos pelo dono (2026-08-28): xai, glm, kimi -------------


def test_xai_glm_kimi_profiles_registered():
    from lohra.providers import get_provider_profile

    xai = get_provider_profile("xai")
    assert xai.base_url == "https://api.x.ai/v1"
    assert xai.env_vars == ("XAI_API_KEY",)
    assert get_provider_profile("grok") is xai  # alias

    glm = get_provider_profile("glm")
    # docs.z.ai: api.z.ai é o host internacional oficial (bigmodel.cn = legado/China)
    assert glm.base_url == "https://api.z.ai/api/paas/v4"
    assert "ZHIPUAI_API_KEY" in glm.env_vars and "ZAI_API_KEY" in glm.env_vars
    assert get_provider_profile("zhipu") is glm

    kimi = get_provider_profile("kimi")
    assert kimi.base_url == "https://api.moonshot.ai/v1"
    assert kimi.env_vars == ("MOONSHOT_API_KEY",)
    assert get_provider_profile("moonshot") is kimi


def test_new_profiles_use_a_registered_transport_and_have_fallbacks():
    from lohra.providers import get_provider_profile
    from lohra.providers.transports import get_transport

    for name in ("xai", "glm", "kimi"):
        profile = get_provider_profile(name)
        assert profile.api_mode == "chat_completions"
        assert get_transport(profile.api_mode) is not None
        assert profile.fallback_models  # configure_for depends on a non-empty default
        assert profile.default_max_tokens


def test_ordering_keeps_anthropic_first_and_ollama_last():
    # Auto-detection walks BUILTIN_PROFILES in order; the new entries must not
    # change who wins detection (anthropic) nor demote keyless ollama from last.
    from lohra.providers.builtin import BUILTIN_PROFILES

    assert BUILTIN_PROFILES[0].name == "anthropic"
    assert BUILTIN_PROFILES[-1].name == "ollama"
    assert {p.name for p in BUILTIN_PROFILES} >= {"xai", "glm", "kimi"}


def test_fallback_slugs_match_the_2026_08_research():
    # Pesquisa online 2026-08-28 (docs.x.ai / docs.z.ai / platform.kimi.ai):
    # grok-4 e grok-3-mini RETIRADOS; linha glm-5.3 é a atual; família kimi-k2
    # original teve EOL em mai/2026. Fallbacks pinados ao que as docs oficiais
    # confirmam ativo hoje.
    from lohra.providers import get_provider_profile

    assert get_provider_profile("xai").fallback_models == ("grok-4.6", "grok-4.3")
    assert get_provider_profile("glm").fallback_models == ("glm-5.3", "glm-5.3-flash")
    assert get_provider_profile("kimi").fallback_models == ("kimi-k3", "kimi-k2.6")
    assert get_provider_profile("kimi").supports_vision  # k3/k2.6 são multimodais


# --- context window metadata (issue #38) ---


def test_profile_context_window_hook_defaults_to_the_profile_default():
    # Mesmo shape de default_max_tokens/get_max_tokens: o perfil DECLARA a janela
    # e o hook é overridável por modelo em perfis futuros.
    from lohra.providers.base import ProviderProfile

    silent = ProviderProfile(name="t-silent")
    assert silent.default_context_window is None
    assert silent.get_context_window("whatever") is None

    declared = ProviderProfile(name="t-declared", default_context_window=32_000)
    assert declared.get_context_window("anything") == 32_000


def test_every_builtin_profile_declares_a_context_window_except_ollama():
    # Sem claim algum, todo modelo herdaria o antigo hardcode de 200k e um
    # modelo de janela menor morreria por `length` sem preflight (issue #38).
    from lohra.providers.builtin import BUILTIN_PROFILES

    for profile in BUILTIN_PROFILES:
        window = profile.get_context_window(profile.fallback_models[0] if profile.fallback_models else "")
        if profile.name == "ollama":
            assert window is None, "ollama é local: o operador escolhe o modelo, não há claim honesto"
        else:
            assert isinstance(window, int) and window > 0, profile.name


def test_the_known_windows_are_the_documented_ones():
    from lohra.providers import get_provider_profile

    # Fatos estáveis: família Claude = 200k; família GPT (gpt-4o/-mini) = 128k.
    assert get_provider_profile("anthropic").get_context_window("claude-opus-4-8") == 200_000
    assert get_provider_profile("openai").get_context_window("gpt-4o") == 128_000


# --- per-model context window (longest-prefix matcher, issue #38) ---


def test_model_windows_matches_the_longest_prefix():
    # Um provider serve modelos de janelas muito diferentes sob o mesmo perfil.
    # O casamento é por prefixo MAIS LONGO: `claude-opus-4-5` (200k) não pode ser
    # confundido com `claude-opus-4-6` (1M), e sufixos de data são absorvidos.
    from lohra.providers.base import ProviderProfile

    p = ProviderProfile(
        name="t-map",
        default_context_window=50_000,
        model_windows={"foo-4-5": 200_000, "foo-4-6": 1_000_000},
    )
    assert p.get_context_window("foo-4-5") == 200_000
    assert p.get_context_window("foo-4-5-20251101") == 200_000  # sufixo de data
    assert p.get_context_window("foo-4-6") == 1_000_000
    # o prefixo `foo-4-5` NÃO casa `foo-4-6`
    assert p.get_context_window("foo-4-6-turbo") == 1_000_000


def test_model_windows_falls_back_to_the_default_when_nothing_matches():
    from lohra.providers.base import ProviderProfile

    p = ProviderProfile(
        name="t-map2", default_context_window=42_000, model_windows={"known": 900_000}
    )
    assert p.get_context_window("desconhecido") == 42_000
    # sem default e sem match → None (comportamento honesto da base)
    silent = ProviderProfile(name="t-map3", model_windows={"known": 900_000})
    assert silent.get_context_window("desconhecido") is None


def test_model_windows_never_matches_on_an_empty_key():
    # Uma chave vazia seria prefixo de TODO modelo — jamais pode casar.
    from lohra.providers.base import ProviderProfile

    p = ProviderProfile(
        name="t-map4", default_context_window=7_000, model_windows={"": 999_999}
    )
    assert p.get_context_window("qualquer-coisa") == 7_000


def test_openrouter_declares_a_conservative_floor_not_an_optimistic_guess():
    # A rota serve centenas de modelos com janelas muito diferentes; o valor real
    # de cada um chega pelo cache do catálogo. O default tem de ser PISO.
    from lohra.providers import get_provider_profile

    openrouter = get_provider_profile("openrouter")
    assert openrouter.get_context_window("deepseek/deepseek-v4-pro") == 32_000
    assert openrouter.default_context_window < get_provider_profile("openai").default_context_window
