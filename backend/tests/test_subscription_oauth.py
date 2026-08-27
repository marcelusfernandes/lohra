"""Tests for first-party OAuth login + own token store + transparent refresh (B5)."""

import base64
import json
import os
import stat

import pytest

from lohra.subscription import manage, oauth, store, token_store
from lohra.subscription.credentials import SubscriptionError, resolve
from lohra.subscription.token_store import OAuthTokens


def _jwt(claims: dict) -> str:
    p = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{p}.s"


# --- token_store ---


def test_token_store_roundtrip_chmod_repr(tmp_path):
    toks = OAuthTokens(access_token="SEKRIT-ACC", refresh_token="SEKRIT-REF", account_id="acct", expires_at=123.0)
    token_store.write_tokens(tmp_path, toks)
    got = token_store.read_tokens(tmp_path)
    assert got == toks
    assert stat.S_IMODE(os.stat(token_store.token_path(tmp_path)).st_mode) == 0o600
    assert "SEKRIT" not in repr(got)  # secrets redacted


def test_token_store_absent_is_none(tmp_path):
    assert token_store.read_tokens(tmp_path) is None


def test_token_file_never_world_readable(tmp_path):
    # overwrite an existing 0644 file → must end at 0600 (atomic write, no window)
    p = token_store.token_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    os.chmod(p, 0o644)
    token_store.write_tokens(tmp_path, OAuthTokens("A", "R", None, 0.0))
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_default_post_sends_user_agent(monkeypatch):
    import httpx

    captured = {}

    def fake_post(url, **kw):
        captured["headers"] = kw.get("headers")

        class _R:
            status_code = 200

            def json(self):
                return {}

        return _R()

    monkeypatch.setattr(httpx, "post", fake_post)
    oauth.default_post(oauth.USERCODE_URL, {"client_id": "x"})
    assert "User-Agent" in captured["headers"] and captured["headers"]["User-Agent"].startswith("lohra/")


def test_token_store_clear(tmp_path):
    token_store.write_tokens(tmp_path, OAuthTokens("A", "R", None, 0.0))
    assert token_store.clear_tokens(tmp_path) is True
    assert token_store.read_tokens(tmp_path) is None
    assert token_store.clear_tokens(tmp_path) is False  # nothing to remove


# --- oauth device flow ---


def _fake_post(usercode=None, device_token=None, token=None):
    def post(url, body):
        if url == oauth.USERCODE_URL:
            return usercode
        if url == oauth.DEVICE_TOKEN_URL:
            return device_token() if callable(device_token) else device_token
        if url == oauth.TOKEN_URL:
            return token
        return 404, None

    return post


def test_device_login_and_poll():
    post = _fake_post(
        usercode=(200, {"device_auth_id": "D", "user_code": "WXYZ", "interval": "1"}),
        device_token=(200, {"authorization_code": "C", "code_verifier": "V"}),
        token=(200, {"access_token": "ACC", "refresh_token": "REF",
                     "id_token": _jwt({"chatgpt_account_id": "acct-1"}), "expires_in": 3600}),
    )
    device = oauth.start_device_login(post)
    assert device.user_code == "WXYZ" and device.verify_url == oauth.DEVICE_VERIFY_URL
    toks = oauth.poll_for_tokens(device, post, sleep=lambda s: None)
    assert toks.access_token == "ACC" and toks.refresh_token == "REF"
    assert toks.account_id == "acct-1" and toks.expires_at > 0


def test_poll_waits_through_pending_then_succeeds():
    seq = iter([(404, None), (403, None),
                (200, {"authorization_code": "C", "code_verifier": "V"})])
    post = _fake_post(
        usercode=(200, {"device_auth_id": "D", "user_code": "X", "interval": "1"}),
        device_token=lambda: next(seq),
        token=(200, {"access_token": "A", "refresh_token": "R", "expires_in": 60}),
    )
    device = oauth.start_device_login(post)
    assert oauth.poll_for_tokens(device, post, sleep=lambda s: None).access_token == "A"


def test_poll_fails_on_hard_error():
    post = _fake_post(
        usercode=(200, {"device_auth_id": "D", "user_code": "X", "interval": "1"}),
        device_token=(500, None),
    )
    device = oauth.start_device_login(post)
    with pytest.raises(oauth.OAuthError):
        oauth.poll_for_tokens(device, post, sleep=lambda s: None)


def test_account_id_from_nested_and_org_claims():
    assert oauth._account_id(_jwt({"chatgpt_account_id": "top"})) == "top"
    assert oauth._account_id(_jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "nested"}})) == "nested"
    assert oauth._account_id(_jwt({"organizations": [{"id": "org-1"}]})) == "org-1"
    assert oauth._account_id("not-a-jwt") is None


def test_refresh_keeps_old_refresh_token_when_not_rotated():
    post = lambda u, b: (200, {"access_token": "NEW", "expires_in": 60})  # no refresh_token  # noqa: E731
    out = oauth.refresh_tokens("OLD-REF", post)
    assert out.access_token == "NEW" and out.refresh_token == "OLD-REF"  # kept


def test_refresh_token_free_error():
    post = lambda u, b: (401, None)  # noqa: E731
    with pytest.raises(oauth.OAuthError) as exc:
        oauth.refresh_tokens("secret-ref", post)
    assert "secret-ref" not in str(exc.value)


# --- resolve precedence + transparent refresh ---


def _enable(home):
    store.write_config(home, store.SubscriptionConfig("subscription", acknowledged_tos_risk=True))


def test_resolve_prefers_own_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))  # no codex login
    _enable(tmp_path)
    token_store.write_tokens(tmp_path, OAuthTokens("OWN", "R", "acct-own", expires_at=1e12))
    creds = resolve(tmp_path)
    assert creds.token == "OWN" and creds.account_id == "acct-own"


def test_resolve_refreshes_own_token_and_persists(tmp_path):
    _enable(tmp_path)
    token_store.write_tokens(tmp_path, OAuthTokens("OLD", "R1", "acct", expires_at=100))  # expired
    post = lambda u, b: (200, {"access_token": "FRESH", "refresh_token": "R2", "expires_in": 3600})  # noqa: E731
    creds = resolve(tmp_path, now=1000, post=post)
    assert creds.token == "FRESH"
    # the rotated family is persisted SAFELY in our own store
    saved = token_store.read_tokens(tmp_path)
    assert saved.access_token == "FRESH" and saved.refresh_token == "R2"
    assert saved.account_id == "acct"  # preserved across refresh


def test_refresh_race_uses_winner_token(tmp_path):
    # concurrent refresh: our refresh fails, but another process already wrote a
    # fresh token → use it instead of erroring (no lock needed for the common race)
    _enable(tmp_path)
    token_store.write_tokens(tmp_path, OAuthTokens("OLD", "R", "acct", expires_at=100))

    def racing_post(u, b):
        # simulate the winner having rotated + persisted a fresh token meanwhile
        token_store.write_tokens(tmp_path, OAuthTokens("WINNER", "R2", "acct", expires_at=1e12))
        return (401, None)  # our own refresh of the now-dead token fails

    creds = resolve(tmp_path, now=1000, post=racing_post)
    assert creds.token == "WINNER"


def test_resolve_refresh_failure_is_subscription_error(tmp_path):
    _enable(tmp_path)
    token_store.write_tokens(tmp_path, OAuthTokens("OLD", "R", "acct", expires_at=100))
    post = lambda u, b: (401, None)  # noqa: E731
    with pytest.raises(SubscriptionError):
        resolve(tmp_path, now=1000, post=post)


def test_status_reports_own_login(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none"))
    _enable(tmp_path)
    token_store.write_tokens(tmp_path, OAuthTokens("A", "R", "acct", expires_at=1e12))
    st = manage.status(tmp_path)
    assert st["own_login"] is True and st["own_login_expired"] is False
