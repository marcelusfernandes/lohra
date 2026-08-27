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
