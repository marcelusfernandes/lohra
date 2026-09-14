"""A cached client resolves one coherent credential snapshot per logical request."""

import json
import traceback

import openai
import pytest

from lohra.agent.client import ResponsesClient
from lohra.subscription import credentials, oauth, store, token_store
from lohra.subscription.credentials import SubscriptionError
from lohra.subscription.provider import build_subscription_client
from tests.subscription_fakes import codex_login, jwt, own_login, pair, sdk_transport


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    state = sdk_transport(monkeypatch, tmp_path)
    yield state
    for client in state.clients:
        client.close()


@pytest.mark.parametrize("method", ["create", "stream"])
def test_same_client_refreshes_and_persists_before_the_next_request(
    tmp_path, monkeypatch, sdk, method
):
    clock = [1000]
    monkeypatch.setattr(credentials.time, "time", lambda: clock[0])
    own_login(tmp_path)
    refreshed = jwt(10000, "account-new")
    posts = []

    def post(url, body):
        posts.append(body)
        return 200, {
            "access_token": refreshed,
            "refresh_token": "synthetic-rotated",
            "expires_in": 5000,
        }

    monkeypatch.setattr(oauth, "default_post", post)
    client = build_subscription_client(tmp_path)
    send = getattr(client, method)
    send(model="synthetic", input="first")
    clock[0] = 2100
    send(model="synthetic", input="second")

    assert [pair(req) for req in sdk.requests] == [
        (["Bearer synthetic-old"], ["account-old"]),
        ([f"Bearer {refreshed}"], ["account-new"]),
    ]
    assert len(posts) == 1 and posts[0]["refresh_token"] == "synthetic-family"
    assert token_store.read_tokens(tmp_path).refresh_token == "synthetic-rotated"
    assert len(sdk.clients) == 1


@pytest.mark.parametrize("source", ["own", "codex"])
def test_new_snapshot_removes_old_account_and_caller_cannot_override_it(
    tmp_path, monkeypatch, sdk, source
):
    monkeypatch.setattr(credentials.time, "time", lambda: 1000)
    path = tmp_path / "codex" / "auth.json"
    monkeypatch.setenv("CODEX_HOME", str(path.parent))
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", True))
    if source == "own":
        own_login(tmp_path)
    else:
        codex_login(path)
    client = build_subscription_client(tmp_path)
    client.create(model="synthetic", input="first")
    if source == "own":
        token_store.write_tokens(
            tmp_path, token_store.OAuthTokens("synthetic-new", "family", None, 10000)
        )
        expected = "synthetic-new"
    else:
        codex_login(path, account=None)
        expected = jwt(10000)
    caller = {
        "authorization": "Bearer unwanted",
        "AUTHORIZATION": openai.Omit(),
        "chatgpt-ACCOUNT-id": "unwanted-account",
        "X-Test": "preserved",
    }
    before = dict(caller)
    client.stream(model="synthetic", input="second", extra_headers=caller)

    assert pair(sdk.requests[-1]) == ([f"Bearer {expected}"], [])
    assert sdk.requests[-1].headers["x-test"] == "preserved" and caller == before
    if source == "codex":
        assert json.loads(path.read_text())["tokens"]["refresh_token"] == "synthetic-codex-family"


@pytest.mark.parametrize("change", ["disabled", "unacknowledged", "api_key"])
def test_cached_client_rechecks_human_gate_without_a_model_request(
    tmp_path, monkeypatch, sdk, change
):
    monkeypatch.setattr(credentials.time, "time", lambda: 1000)
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    store.write_config(
        tmp_path,
        store.SubscriptionConfig(
            "api_key" if change == "disabled" else "subscription",
            change != "unacknowledged",
            preference="api_key" if change == "api_key" else "auto",
        ),
    )
    with pytest.raises(SubscriptionError):
        client.create(model="synthetic", input="never sent")
    assert sdk.requests == []


def test_refresh_transport_failure_is_token_free_and_prevents_model_send(
    tmp_path, monkeypatch, sdk
):
    clock = [1000]
    monkeypatch.setattr(credentials.time, "time", lambda: clock[0])
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)

    def post(*_):
        raise RuntimeError("PRIVATE-REFRESH-CANARY")

    monkeypatch.setattr(oauth, "default_post", post)
    clock[0] = 2100
    with pytest.raises(SubscriptionError) as caught:
        client.create(model="synthetic", input="never sent")
    assert "CANARY" not in "".join(traceback.format_exception(caught.value))
    assert "auth login" in str(caught.value) and sdk.requests == []


@pytest.mark.parametrize("dynamic", [False, True])
def test_only_dynamic_subscription_disables_sdk_model_replay(tmp_path, monkeypatch, sdk, dynamic):
    monkeypatch.setattr(credentials.time, "time", lambda: 1000)
    own_login(tmp_path)
    client = (
        build_subscription_client(tmp_path)
        if dynamic
        else ResponsesClient(api_key="synthetic-static", base_url="https://synthetic.invalid/v1")
    )
    sdk.handler = lambda req: sdk.http.Response(
        500, headers={"retry-after-ms": "1"}, json={"error": {"message": "synthetic failure"}}
    )
    with pytest.raises(openai.InternalServerError):
        client.create(model="synthetic", input="one logical request")
    assert len(sdk.requests) == (1 if dynamic else 3)
    assert client._client.max_retries == (0 if dynamic else 2)


def test_local_auth_refusal_is_typed_and_never_quota():
    from lohra.providers.errors import AUTH_FAILED, classify_provider_error
    from lohra.workflow.leaf_retry import is_retryable_failure

    error = SubscriptionError("arbitrary wording")
    assert classify_provider_error(error) == AUTH_FAILED
    assert not is_retryable_failure("error", classify_provider_error(error))
    assert classify_provider_error(RuntimeError(str(error))) is None


def test_dynamic_callback_removes_conflicting_sdk_defaults(tmp_path, monkeypatch, sdk):
    client = ResponsesClient(
        api_key="initial",
        base_url="https://synthetic.invalid",
        default_headers={
            "authorization": "Bearer unwanted",
            "CHATGPT-Account-id": "stale",
            "Originator": "wrong",
            "X-Static": "kept",
        },
        credential_headers=lambda: {"Authorization": "Bearer current", "originator": "lohra"},
    )
    client.create(model="synthetic", input="go")
    assert pair(sdk.requests[-1]) == (["Bearer current"], [])
    assert sdk.requests[-1].headers.get_list("originator") == ["lohra"]
    assert sdk.requests[-1].headers["x-static"] == "kept"


@pytest.mark.parametrize("advance_during", ["refresh", "persist"])
def test_refresh_rechecks_clock_after_io_before_a_model_send(
    tmp_path, monkeypatch, sdk, advance_during
):
    clock = [1000]
    monkeypatch.setattr(credentials.time, "time", lambda: clock[0])
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    clock[0] = 2100

    def post(*_):
        # Explicit absolute expiry models a response that spent time in flight.
        if advance_during == "refresh":
            clock[0] = 10000
        return token_store.OAuthTokens("new", "rotated", None, 6000)

    monkeypatch.setattr(oauth, "refresh_tokens", post)
    if advance_during == "persist":
        writer = token_store._write_tokens_locked

        def persist(home, tokens):
            writer(home, tokens)
            clock[0] = 10000

        monkeypatch.setattr(token_store, "_write_tokens_locked", persist)
    with pytest.raises(SubscriptionError):
        client.create(model="synthetic", input="never expired")
    assert sdk.requests == []


def test_real_sdk_debug_logs_do_not_disclose_request_bearer(tmp_path, monkeypatch, sdk, caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="openai")
    caplog.set_level(logging.DEBUG, logger="httpx2")
    caplog.set_level(logging.DEBUG, logger="httpx")
    monkeypatch.setattr(credentials.time, "time", lambda: 1000)
    secret = "SYNTHETIC-BEARER-LOG-CANARY"
    own_login(tmp_path, token=secret)
    client = build_subscription_client(tmp_path)
    client.create(model="synthetic", input="log test")
    assert sdk.requests[0].headers["authorization"] == f"Bearer {secret}"
    assert caplog.records and secret not in caplog.text
