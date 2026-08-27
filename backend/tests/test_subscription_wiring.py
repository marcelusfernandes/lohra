"""Tests for B3 wiring: lohra auth, provider/client builder, serve gating (Fase 10)."""

import base64
import json

import pytest

from lohra.subscription import manage, store
from lohra.subscription.credentials import SubscriptionError
from lohra.subscription.provider import CODEX_PROVIDER, build_subscription_client


def _jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"h.{payload}.s"


def _write_codex_auth(path, access):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": {
        "access_token": access, "refresh_token": "r", "account_id": "acct"}}))


# --- manage (lohra auth) ---


def test_enable_then_disable(tmp_path):
    manage.enable(tmp_path)
    cfg = store.read_config(tmp_path)
    assert cfg.active is True and cfg.acknowledged_tos_risk is True
    manage.disable(tmp_path)
    assert store.read_config(tmp_path).active is False


def test_status_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", _jwt(99_999_999_999))
    manage.enable(tmp_path)
    st = manage.status(tmp_path)
    assert st["active"] is True and st["codex_login_found"] is True
    assert st["codex_token_expired"] is False and st["account_id"] == "acct"


def test_status_no_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    st = manage.status(tmp_path)
    assert st["codex_login_found"] is False and st["active"] is False


def test_tos_warning_mentions_ban():
    assert "BAN" in manage.TOS_WARNING.upper()


# --- provider / client builder ---


def test_codex_provider_is_responses_mode():
    assert CODEX_PROVIDER.api_mode == "responses" and CODEX_PROVIDER.requires_api_key is False


def test_codex_aux_model_falls_back_to_resolved():
    # aux (compaction/titles) must NOT pin gpt-5.5 — an account on another slug 400s
    assert CODEX_PROVIDER.default_aux_model == ""


def test_read_codex_model_from_config(tmp_path, monkeypatch):
    from lohra.subscription.codex_creds import read_codex_model

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n')
    assert read_codex_model() == "gpt-5.5"


def test_codex_default_model_falls_back(tmp_path, monkeypatch):
    from lohra.subscription.provider import DEFAULT_CODEX_MODEL, codex_default_model

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))  # no config.toml
    assert codex_default_model() == DEFAULT_CODEX_MODEL


def test_build_subscription_client_off_raises(tmp_path):
    with pytest.raises(SubscriptionError):
        build_subscription_client(tmp_path)  # mode off -> not active


def test_build_subscription_client_constructs(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", _jwt(99_999_999_999))
    manage.enable(tmp_path)

    import openai

    captured = {}

    class _Fake:
        def __init__(self, **kw):
            captured.update(kw)
            self.responses = object()

    monkeypatch.setattr(openai, "OpenAI", _Fake)
    client = build_subscription_client(tmp_path, now=2000)
    assert captured["base_url"].endswith("/codex")
    assert captured["default_headers"]["ChatGPT-Account-ID"] == "acct"
    assert client is not None


# --- serve gating ---


def test_dashboard_reaches_subscription_path(tmp_path, monkeypatch):
    # a subscription-only desktop user (no API key) must NOT fall into the key path
    from lohra import cli

    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    manage.enable(tmp_path)

    def _boom(home, **kw):
        raise SubscriptionError("SENTINEL")

    monkeypatch.setattr("lohra.subscription.provider.build_subscription_client", _boom)
    manager, app, code = cli.build_dashboard_app(insecure=True)
    assert code == 2 and app is None  # reached subscription branch, surfaced its error


def test_serve_refuses_when_subscription_active(tmp_path, monkeypatch):
    from lohra import cli

    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)
    manage.enable(tmp_path)
    code = cli.run_openai_server(host="127.0.0.1", port=9, insecure=True)
    assert code == 2  # refused — never exposes the subscription token


def test_subscription_token_never_enters_os_environ(tmp_path, monkeypatch):
    # B4 robustness: the bearer is passed to the SDK client directly, never exported
    # to the environment (where it could leak into subprocesses / the prompt).
    import os

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", _jwt(99_999_999_999))
    manage.enable(tmp_path)

    import openai

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: type("C", (), {"responses": None})())
    before = dict(os.environ)
    build_subscription_client(tmp_path, now=2000)
    assert dict(os.environ) == before  # no token (or anything) exported to env


def test_run_chat_reaches_subscription_path_without_api_key(tmp_path, monkeypatch):
    # The primary path: subscription on, NO API key. run_chat must reach the
    # subscription branch (not the "set an API key" early-exit). We prove it by
    # making the subscription client builder raise a sentinel and asserting THAT
    # error surfaces (exit 2), not the provider-resolution error.
    from lohra import cli

    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    manage.enable(tmp_path)

    def _boom(home, **kw):
        raise SubscriptionError("SENTINEL-reached-subscription")

    monkeypatch.setattr("lohra.subscription.provider.build_subscription_client", _boom)
    code = cli.run_chat("hello")
    assert code == 2  # reached subscription branch + surfaced its error (not "set an API key")
