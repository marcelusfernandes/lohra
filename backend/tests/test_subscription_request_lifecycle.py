"""Cached/borrowed client lifetime and real harness refusal, entirely offline."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import traceback

import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client_pool import ClientPool
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.subscription import credentials, store, token_store
from lohra.subscription.errors import SubscriptionError
from lohra.subscription.provider import CODEX_PROVIDER, build_subscription_client
from tests.subscription_fakes import codex_login, jwt, own_login, pair, sdk_transport


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    state = sdk_transport(monkeypatch, tmp_path)
    monkeypatch.setattr(credentials.time, "time", lambda: 1000)
    yield state
    for client in state.clients:
        client.close()


def test_concurrent_requests_keep_snapshot_while_store_and_other_request_advance(tmp_path, sdk):
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    original_key = client._client.api_key
    original_headers = dict(client._client._custom_headers)
    first_started, second_finished = threading.Event(), threading.Event()

    def handle(request):
        if json.loads(request.content)["input"] == "first":
            first_started.set()
            assert second_finished.wait(5)  # model HTTP must not hold the auth lock
        return sdk.http.Response(200, headers={"content-type": "text/event-stream"}, content=b"")

    sdk.handler = handle
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(client.create, model="synthetic", input="first")
        assert first_started.wait(3)
        token_store.write_tokens(
            tmp_path, token_store.OAuthTokens("new", "family2", "new-account", 10000)
        )
        second = pool.submit(client.stream, model="synthetic", input="second")
        try:
            second.result(3)
        finally:
            second_finished.set()
        first.result(3)
    assert [pair(r) for r in sdk.requests] == [
        (["Bearer synthetic-old"], ["account-old"]),
        (["Bearer new"], ["new-account"]),
    ]
    assert client._client.api_key == original_key
    assert dict(client._client._custom_headers) == original_headers


def test_open_stream_keeps_original_snapshot_and_next_stream_rereads(tmp_path, sdk):
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)

    class ChangingStream(sdk.http.SyncByteStream):
        def __iter__(self):
            token_store.write_tokens(
                tmp_path, token_store.OAuthTokens("new", "family2", None, 10000)
            )
            yield b'data: {"type":"response.output_text.delta","delta":"old-stream"}\n\n'

    sdk.handler = lambda request: sdk.http.Response(
        200, headers={"content-type": "text/event-stream"}, stream=ChangingStream()
    )
    texts = []
    client.stream(model="synthetic", input="first", on_text=texts.append)
    client.stream(model="synthetic", input="second")
    assert texts == ["old-stream"]
    assert [pair(r) for r in sdk.requests] == [
        (["Bearer synthetic-old"], ["account-old"]),
        (["Bearer new"], []),
    ]


@pytest.mark.parametrize("borrowed", [True, False])
def test_pool_binds_home_and_rechecks_gate_of_cached_and_borrowed_client(
    tmp_path, monkeypatch, sdk, borrowed
):
    home = tmp_path / "original"
    own_login(home)
    monkeypatch.chdir(tmp_path)
    if borrowed:
        parent = build_subscription_client(Path("original"))
        pool = ClientPool(CODEX_PROVIDER, parent, Path("original"))
    else:
        parent = object()
        pool = ClientPool(get_provider_profile("anthropic"), parent, Path("original"))
    other = tmp_path / "other"
    own_login(other, token="wrong-profile")
    monkeypatch.chdir(other)
    for variable in ("HOME", "LOHRA_HOME", "CODEX_HOME"):
        monkeypatch.setenv(variable, str(other))
    monkeypatch.setenv("LOHRA_PROFILE", "other")
    client = pool.get("openai-codex")[1]
    client.create(model="synthetic", input="bound")
    assert pair(sdk.requests[-1])[0] == ["Bearer synthetic-old"]
    assert pool.get("openai-codex")[1] is client
    store.write_config(home, store.SubscriptionConfig("subscription", True, preference="api_key"))
    with pytest.raises(SubscriptionError):
        pool.get("openai-codex")[1].stream(model="synthetic", input="refused")
    assert len(sdk.requests) == 1
    pool.close()
    assert client._client.is_closed() is not borrowed


def test_codex_source_path_is_bound_but_contents_are_live_and_never_written(
    tmp_path, monkeypatch, sdk
):
    original = tmp_path / "original" / "auth.json"
    codex_login(original)
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", True))
    monkeypatch.setenv("CODEX_HOME", str(original.parent))
    client = build_subscription_client(tmp_path)
    decoy = tmp_path / "decoy" / "auth.json"
    codex_login(decoy, account="wrong-account")
    monkeypatch.setenv("CODEX_HOME", str(decoy.parent))
    codex_login(original, account=None)
    before = original.read_bytes()
    client.create(model="synthetic", input="still original")
    assert pair(sdk.requests[-1]) == ([f"Bearer {jwt(10000)}"], [])
    assert original.read_bytes() == before
    codex_login(original, expiry=900)
    expired = original.read_bytes()
    with pytest.raises(SubscriptionError, match="Codex token is expired"):
        client.stream(model="synthetic", input="expired")
    assert len(sdk.requests) == 1 and original.read_bytes() == expired
    assert not token_store.token_path(tmp_path).exists()


@pytest.mark.parametrize("status", [401, 403, 429])
def test_sdk_auth_is_sanitized_and_not_replayed_but_quota_stays_quota(tmp_path, sdk, status):
    from lohra.providers.errors import classify_provider_error

    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    sdk.handler = lambda request: sdk.http.Response(
        status, json={"error": {"message": "PRIVATE-PROVIDER-CANARY"}}
    )
    expected = openai.RateLimitError if status == 429 else SubscriptionError
    with pytest.raises(expected) as caught:
        client.create(model="synthetic", input="refused")
    assert classify_provider_error(caught.value) == (
        "quota_exhausted" if status == 429 else "auth_failed"
    )
    assert len(sdk.requests) == 1
    if status != 429:
        assert "CANARY" not in "".join(traceback.format_exception(caught.value))


def test_pre_responses_sdk_refuses_only_dynamic_subscription(tmp_path, monkeypatch, sdk):
    from lohra.agent.client import OpenAIClient

    own_login(tmp_path)

    class OldSDK:
        def __init__(self, **kwargs):
            self.closed = False

        def close(self):
            self.closed = True

    clients = []

    def build(**kwargs):
        client = OldSDK(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", build)
    with pytest.raises(SubscriptionError, match="openai>=1.66.0"):
        build_subscription_client(tmp_path)
    assert clients[0].closed
    static = OpenAIClient(api_key="synthetic", base_url="https://synthetic.invalid")
    assert not clients[1].closed
    static.close()


def test_real_leaf_auth_failure_pauses_without_respawn_or_paid_fallback(tmp_path, sdk):
    from lohra.workflow.service import WorkflowService

    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    built = []

    def factory():
        built.append(True)
        return Agent(model="synthetic", provider=CODEX_PROVIDER, client=client)

    db = SessionDB(str(tmp_path / "synthetic.db"))
    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    store.write_config(tmp_path, store.SubscriptionConfig("api_key", False))
    try:
        started = service.start(
            spec_dict={
                "meta": {"name": "auth"},
                "nodes": [
                    {"id": "leaf", "type": "agent", "prompt": "go", "retries": 3},
                ],
            },
            args={},
        )
        result = service.status(started["run_id"], wait=True, timeout=5)
        assert result["status"] == "paused"
        assert result["reason"] == "route_fault"
        assert len(built) == 1 and sdk.requests == []
        assert "auth login" in json.dumps(result) or "auth enable" in json.dumps(result)
    finally:
        service.shutdown()
        db.close()
