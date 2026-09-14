"""Synthetic subscription state and real-SDK HTTP transport, never providers."""

import base64
import inspect
import json
from types import SimpleNamespace

import openai

from lohra.subscription import oauth, store, token_store


def jwt(expiry, account=None):
    claims = {"exp": expiry}
    if account is not None:
        claims["chatgpt_account_id"] = account
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"synthetic.{payload}.signature"


def own_login(home, *, expiry=2000, account="account-old", token="synthetic-old"):
    store.write_config(home, store.SubscriptionConfig("subscription", True))
    token_store.write_tokens(
        home, token_store.OAuthTokens(token, "synthetic-family", account, expiry)
    )


def codex_login(path, *, expiry=10000, account="account-old"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": jwt(expiry, account),
                    "account_id": account,
                    "refresh_token": "synthetic-codex-family",
                }
            }
        )
    )


def no_network(*_args, **_kwargs):
    raise AssertionError("real credential/network access forbidden")


def sdk_transport(monkeypatch, tmp_path):
    if "httpx2" in str(inspect.signature(openai.OpenAI)):
        import httpx2 as http
    else:
        import httpx as http
    state = SimpleNamespace(http=http, requests=[], clients=[], handler=None)

    def handle(request):
        state.requests.append(request)
        if state.handler is not None:
            return state.handler(request)
        event = {
            "type": "response.completed",
            "response": {
                "id": "synthetic",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        }
        return http.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(event)}\n\n",
        )

    sdk_class = openai.OpenAI

    def factory(**kwargs):
        client = sdk_class(**kwargs, http_client=http.Client(transport=http.MockTransport(handle)))
        state.clients.append(client)
        return client

    monkeypatch.setattr(openai, "OpenAI", factory)
    monkeypatch.setattr(oauth, "default_post", no_network)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unused-codex"))
    return state


def pair(request):
    return request.headers.get_list("authorization"), request.headers.get_list("chatgpt-account-id")
