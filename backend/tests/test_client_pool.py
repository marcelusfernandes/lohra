"""Tests for cross-provider delegation: ClientPool + configure_for (Fase 10+)."""

import pytest

from lohra.agent.client_pool import ClientPool, ProviderError, configure_for
from lohra.providers.base import ProviderProfile


_PARENT = ProviderProfile(name="anthropic", api_mode="anthropic_messages", fallback_models=("claude-x",))


class _FakeClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _pool(home):
    return ClientPool(parent_provider=_PARENT, parent_client=_FakeClient(), home=home)


# --- ClientPool ---


def test_parent_and_empty_name_return_borrowed(tmp_path):
    pool = _pool(tmp_path)
    parent_client = pool._parent_client
    assert pool.get(None) == (_PARENT, parent_client)
    assert pool.get("anthropic") == (_PARENT, parent_client)  # parent name → borrowed


def test_unknown_provider_errors(tmp_path):
    with pytest.raises(ProviderError):
        _pool(tmp_path).get("nope-provider")


def test_no_credential_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError) as exc:
        _pool(tmp_path).get("openai")
    assert "no API key" in str(exc.value)


def test_builds_and_caches_owned_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    built = []

    def fake_build(profile, env=None):
        c = _FakeClient()
        built.append(c)
        return c

    monkeypatch.setattr("lohra.agent.client_pool.build_client", fake_build)
    pool = _pool(tmp_path)
    p1, c1 = pool.get("openai")
    p2, c2 = pool.get("openai")
    assert c1 is c2 and len(built) == 1  # built once, cached
    assert p1.name == "openai"


def test_close_closes_owned_not_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("lohra.agent.client_pool.build_client", lambda p, env=None: _FakeClient())
    pool = _pool(tmp_path)
    parent_client = pool._parent_client
    _, owned = pool.get("openai")
    pool.close()
    assert owned.closed is True  # built client closed
    assert parent_client.closed is False  # borrowed parent NOT closed


def test_subscription_gate_blocks_when_inactive(tmp_path):
    # provider=openai-codex must NOT auto-escalate when subscription isn't enabled
    with pytest.raises(ProviderError) as exc:
        _pool(tmp_path).get("openai-codex")
    assert "subscription not enabled" in str(exc.value)


# --- configure_for ---


def test_configure_for_none_when_no_override(tmp_path):
    assert configure_for(_pool(tmp_path)) is None


def test_configure_for_swaps_trio(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    oai_client = _FakeClient()
    monkeypatch.setattr("lohra.agent.client_pool.build_client", lambda p, env=None: oai_client)
    pool = _pool(tmp_path)
    configure = configure_for(pool, provider="openai")  # no model → defaults to target fallback
    agent = type("A", (), {"model": "claude-x", "provider": _PARENT, "client": None,
                           "effort": None, "forced_tool": None})()
    configure(agent)
    assert agent.provider.name == "openai" and agent.client is oai_client
    assert agent.model  # defaulted from the target provider's fallback_models


def test_configure_for_provider_error_propagates(tmp_path):
    with pytest.raises(ProviderError):
        configure_for(_pool(tmp_path), provider="ghost")


def test_configure_for_no_default_model_errors(tmp_path, monkeypatch):
    # ollama is buildable (keyless) but has NO fallback_models — a provider override
    # with no model must raise (not leave the parent's foreign slug → 400).
    monkeypatch.setattr("lohra.agent.client_pool.build_client", lambda p, env=None: _FakeClient())
    with pytest.raises(ProviderError) as exc:
        configure_for(_pool(tmp_path), provider="ollama")  # no model
    assert "no default model" in str(exc.value)
