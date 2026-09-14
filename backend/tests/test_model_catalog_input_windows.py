"""#41 preparation: synthetic listing -> on-disk cache -> local resolver.

Reusable tests only. No provider inference, account metadata or seed change.
The optional LOHRA41_EXPECTED_BACKEND assertion pins the preparation checkout.
"""

import os
from pathlib import Path
import socket

import httpx
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.catalog import catalog as cat, windows
from lohra.providers import get_provider_profile
from lohra.subscription.provider import CODEX_PROVIDER


class NoInference(ModelClient):
    def create(self, **kwargs):
        raise AssertionError("inference is outside the catalog contract")


def agent(provider, model, **kwargs):
    profile = get_provider_profile(provider) if isinstance(provider, str) else provider
    return Agent(provider=profile, model=model, client=NoInference(), **kwargs)


@pytest.fixture(autouse=True)
def isolated_catalog(tmp_path, monkeypatch):
    expected = os.environ.get("LOHRA41_EXPECTED_BACKEND")
    if expected:
        assert Path(cat.__file__).resolve() == Path(expected) / "lohra/catalog/catalog.py"
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("unexpected network")
        raise AssertionError("inject the closed MockTransport catalog client")

    monkeypatch.setattr(cat, "default_http_client", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    windows.clear_cache()
    try:
        yield tmp_path
    finally:
        windows.clear_cache()
        assert attempts == []  # catches even an attempted fetch swallowed as fallback


def listing(home, rows, *, provider="anthropic"):
    urls = {
        "anthropic": "https://api.anthropic.com/v1/models?limit=1000",
        "openai": "https://api.openai.com/v1/models",
        "openrouter": "https://openrouter.ai/api/v1/models",
    }
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(200, json={"data": rows, "has_more": False})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = cat.build_catalog(
            env={f"{provider.upper()}_API_KEY": "synthetic-only"}, home=home,
            providers=(provider,), client=client,
        ).get(provider)
    assert client.is_closed
    assert requests == [urls[provider]]  # exactly one listing, no per-model fetch
    assert result.source == "live"
    return result, requests


def resolve_locally(monkeypatch, *agents):
    attempts = []

    def denied(*args, **kwargs):
        attempts.append("catalog called from resolver")
        raise AssertionError("the resolver must only read local cache")

    # The ambient HTTP constructor and sockets are already denied by the fixture.
    monkeypatch.setattr(cat, "fetch_models", denied)
    monkeypatch.setattr(cat, "build_catalog", denied)
    answers = [[item.resolve_context_window() for _ in range(20)] for item in agents]
    assert attempts == []  # do not accept an exception hidden by a fallback
    assert all(len(set(values)) == 1 for values in answers)
    return [values[0] for values in answers]


@pytest.mark.parametrize("metadata, expected", [
    ({"max_input_tokens": 123456, "max_tokens": 512}, 123456),
    ({"context_length": 300000, "top_provider": {"max_input_tokens": 65536}}, 65536),
    ({"max_input_tokens": 32768, "top_provider": {"max_context_length": 65536}}, 32768),
    ({"max_input_tokens": 300000, "top_provider": {"context_length": 65536}}, 65536),
], ids=["new-input-only", "nested-input-smaller", "top-input-smaller", "existing-route-smaller"])
def test_input_window_flows_through_listing_disk_cache_and_local_resolver(
    tmp_path, monkeypatch, metadata, expected,
):
    model = "issue41-unseeded-model"
    entry, requests = listing(tmp_path, [{"id": model, **metadata}])
    assert entry.models == (model,)
    assert entry.head(0).context_lengths == entry.context_lengths
    assert set(entry.to_dict()) == {"provider", "source", "total", "models"}
    windows.clear_cache()  # force a disk read, not an in-process write memo
    persisted = windows.load_windows(tmp_path).get("anthropic", {}).get(model)
    resolved, override = resolve_locally(
        monkeypatch, agent("anthropic", model), agent("anthropic", model, context_window=4242),
    )
    assert override == 4242 and len(requests) == 1
    # Read all three stages before asserting: the baseline evidence identifies
    # the omission in listing, its missing cache entry and the resulting fallback.
    observed = {"listing": entry.context_lengths.get(model), "disk": persisted, "resolver": resolved}
    assert observed == {"listing": expected, "disk": expected, "resolver": expected}


@pytest.mark.parametrize("metadata", [
    {"max_input_tokens": True}, {"max_input_tokens": False},
    {"max_input_tokens": 0}, {"max_input_tokens": -1},
    {"max_input_tokens": 123.5}, {"max_input_tokens": "123456"},
    {"max_input_tokens": None}, {},
], ids=["true", "false", "zero", "negative", "float", "string", "null", "missing"])
def test_invalid_input_metadata_preserves_valid_alternates_and_other_rows(
    tmp_path, monkeypatch, metadata,
):
    entry, requests = listing(tmp_path, [
        {"id": "invalid", "max_tokens": 8192, **metadata},
        {"id": "alternate", "max_context_length": 16384, **metadata},
        {"id": "good", "context_length": 123456},
    ])
    expected = {"alternate": 16384, "good": 123456}
    assert entry.context_lengths == expected
    windows.clear_cache()
    assert windows.load_windows(tmp_path) == {"anthropic": expected}
    fallback, alternate = resolve_locally(
        monkeypatch, agent("anthropic", "invalid"), agent("anthropic", "alternate"),
    )
    assert fallback == get_provider_profile("anthropic").default_context_window
    assert alternate == 16384 and len(requests) == 1


def test_output_limit_alone_does_not_create_an_input_window(tmp_path, monkeypatch):
    entry, requests = listing(tmp_path, [{"id": "output-only", "max_tokens": 8192}])
    assert entry.context_lengths == {}
    assert not windows.windows_path(tmp_path).exists()
    item = agent("anthropic", "output-only", max_tokens=2048)
    assert item.resolve_max_tokens() == 2048  # independent output override
    assert resolve_locally(monkeypatch, item) == [item.provider.default_context_window]
    assert len(requests) == 1


def test_existing_openrouter_route_minimum_remains_conservative(tmp_path, monkeypatch):
    entry, requests = listing(tmp_path, [
        {"id": "vendor/model", "context_length": 500000,
         "top_provider": {"context_length": 32000}},
    ], provider="openrouter")
    assert entry.context_lengths == {"vendor/model": 32000}
    windows.clear_cache()
    assert windows.lookup("openrouter", "vendor/model", home=tmp_path) == 32000
    assert resolve_locally(monkeypatch, agent("openrouter", "vendor/model")) == [32000]
    assert len(requests) == 1


def test_basic_openai_listing_preserves_fallback_and_api_subscription_isolation(tmp_path, monkeypatch):
    entry, requests = listing(tmp_path, [
        {"id": "gpt-5.5", "object": "model", "created": 1, "owned_by": "synthetic"},
    ], provider="openai")
    assert entry.context_lengths == {} and not windows.windows_path(tmp_path).exists()
    api, subscription = agent("openai", "gpt-5.5"), agent(CODEX_PROVIDER, "gpt-5.5")
    # These are the checked-out profiles' static contract, not live provider numbers.
    assert api.resolve_context_window() == api.provider.get_context_window(api.model)
    before = subscription.resolve_context_window()
    assert before == CODEX_PROVIDER.default_context_window
    assert windows.remember_windows({"openai": {"gpt-5.5": 777777}}, home=tmp_path)
    windows.clear_cache()
    assert windows.lookup("openai-codex", "gpt-5.5", home=tmp_path) is None
    assert resolve_locally(monkeypatch, api, subscription) == [777777, before]
    assert len(requests) == 1
