"""Tests for the OpenAI/Codex subscription credential subsystem (Fase 10, B1)."""

import base64
import json
import os
import stat

import pytest

from lohra.subscription import store
from lohra.subscription.codex_creds import CodexTokens, codex_auth_path, read_codex_tokens
from lohra.subscription.constants import ACCOUNT_ID_HEADER, CODEX_BASE_URL
from lohra.subscription.credentials import SubscriptionError, resolve, subscription_active
from lohra.subscription.refresh import RefreshError, is_expired, refresh_access_token


def _jwt(exp: int) -> str:
    head = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{head}.{payload}.sig"


def _write_codex_auth(path, access="acc-tok", refresh="ref-tok", account="acct-1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": access, "refresh_token": refresh, "account_id": account},
    }))


# --- codex_creds ---


def test_codex_auth_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert codex_auth_path() == tmp_path / "cx" / "auth.json"


def test_read_codex_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _write_codex_auth(tmp_path / "auth.json")
    toks = read_codex_tokens()
    assert toks == CodexTokens("acc-tok", "ref-tok", "acct-1")


def test_read_codex_tokens_absent_is_none(tmp_path):
    assert read_codex_tokens(tmp_path / "nope.json") is None


def test_read_codex_tokens_malformed_is_none(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text("not json{")
    assert read_codex_tokens(p) is None


def test_read_codex_tokens_no_access_is_none(tmp_path):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
    assert read_codex_tokens(p) is None


# --- store ---


def test_store_roundtrip_and_chmod(tmp_path):
    cfg = store.SubscriptionConfig(auth_mode="subscription", acknowledged_tos_risk=True)
    store.write_config(tmp_path, cfg)
    got = store.read_config(tmp_path)
    assert got.auth_mode == "subscription" and got.acknowledged_tos_risk is True
    assert got.active is True
    mode = stat.S_IMODE(os.stat(store.auth_path(tmp_path)).st_mode)
    assert mode == 0o600


def test_store_default_off(tmp_path):
    assert store.read_config(tmp_path) is None
    assert subscription_active(tmp_path) is False


def test_store_acknowledged_false_is_not_active(tmp_path):
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=False))
    assert store.read_config(tmp_path).active is False


# --- refresh / expiry ---


def test_is_expired(tmp_path):
    assert is_expired(_jwt(1000), now=2000) is True
    assert is_expired(_jwt(10_000), now=2000) is False
    assert is_expired("garbage") is True  # unparseable -> refresh


def test_refresh_access_token_captures_rotated_token():
    seen = {}

    def post(url, body):
        seen["url"], seen["body"] = url, body
        return {"access_token": "fresh-tok", "refresh_token": "rotated-ref"}

    result = refresh_access_token("ref-tok", post)
    assert result.access_token == "fresh-tok"
    assert result.refresh_token == "rotated-ref"  # server rotates; we keep it (B4)
    assert seen["body"]["grant_type"] == "refresh_token"
    assert seen["body"]["refresh_token"] == "ref-tok"


def test_refresh_result_repr_is_redacted():
    r = refresh_access_token("ref-tok", lambda u, b: {"access_token": "A", "refresh_token": "B"})
    assert "A" not in repr(r) and "B" not in repr(r)


def test_refresh_errors_are_token_free():
    def boom(url, body):
        raise RuntimeError("network down with secret ref-tok in it")

    with pytest.raises(RefreshError) as exc:
        refresh_access_token("ref-tok", boom)
    assert "ref-tok" not in str(exc.value)  # token never leaks into the message


def test_refresh_no_token():
    with pytest.raises(RefreshError):
        refresh_access_token("", lambda u, b: {})


# --- resolve (gate + flow) ---


def test_resolve_off_is_none(tmp_path):
    assert resolve(tmp_path) is None


def test_resolve_requires_tos_ack(tmp_path):
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=False))
    with pytest.raises(SubscriptionError):
        resolve(tmp_path)


def test_resolve_no_codex_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=True))
    with pytest.raises(SubscriptionError) as exc:
        resolve(tmp_path)
    assert "not logged in" in str(exc.value)  # no own login AND no codex login


def test_resolve_returns_creds_with_valid_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", access=_jwt(10_000))
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=True))
    creds = resolve(tmp_path, now=2000)
    assert creds.base_url == CODEX_BASE_URL
    assert creds.headers[ACCOUNT_ID_HEADER] == "acct-1"
    assert creds.token == _jwt(10_000)  # not expired -> used as-is


def test_resolve_raises_on_expired_token_without_touching_codex(tmp_path, monkeypatch):
    # expired -> raise (do NOT refresh here: refresh rotates the token; refreshing
    # without writing the rotated value back would brick Codex's login). Codex owns it.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", access=_jwt(1000))  # expired vs now=2000
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=True))
    with pytest.raises(SubscriptionError) as exc:
        resolve(tmp_path, now=2000)
    assert "expired" in str(exc.value)
    on_disk = json.loads((tmp_path / "cx" / "auth.json").read_text())
    assert on_disk["tokens"]["access_token"] == _jwt(1000)  # Codex file untouched


def test_resolve_creds_repr_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    _write_codex_auth(tmp_path / "cx" / "auth.json", access=_jwt(10_000))
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", acknowledged_tos_risk=True))
    creds = resolve(tmp_path, now=2000)
    assert _jwt(10_000) not in repr(creds) and "***" in repr(creds)


def test_codex_tokens_repr_is_redacted():
    assert "SECRET" not in repr(CodexTokens("SECRET-A", "SECRET-B", "acct"))


def test_gate_fails_closed_on_non_bool_ack(tmp_path):
    # a hostile/typo'd auth.json with acknowledged_tos_risk: "false" (string) or 1
    # must NOT activate the ToS-risky mode (fail closed).
    for bad in ["false", "true", 1, "yes", []]:
        (tmp_path / "auth.json").write_text(
            json.dumps({"openai": {"auth_mode": "subscription", "acknowledged_tos_risk": bad}})
        )
        assert store.read_config(tmp_path).active is False
