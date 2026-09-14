"""Contention, atomic commits and sanitized failures of the actual auth stores."""

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import multiprocessing
from pathlib import Path
import stat
import threading
import time
import traceback

import pytest

from lohra.subscription import credentials, manage, oauth, persistence, store, token_store
from lohra.subscription.errors import SubscriptionError
from tests.subscription_fakes import jwt, own_login, sdk_transport


def _hold_lock(home, entered):
    with persistence.profile_transaction(Path(home)):
        entered.set()
        time.sleep(20)


def test_lock_released_after_process_death_and_exception(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(target=_hold_lock, args=(str(tmp_path), entered))
    process.start()
    try:
        assert entered.wait(5)
    finally:
        process.terminate()
        process.join(5)
    with pytest.raises(KeyboardInterrupt):
        with persistence.profile_transaction(tmp_path):
            raise KeyboardInterrupt
    own_login(tmp_path)  # can reacquire after both forms of exit
    assert store.read_config(tmp_path).active


@pytest.mark.parametrize("operation", ["login", "logout", "disable", "prefer"])
def test_refresh_serializes_with_public_auth_mutations(tmp_path, operation):
    own_login(tmp_path, expiry=1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def post(*_):
        entered.set()
        assert release.wait(5)
        return 200, {"access_token": "refreshed", "refresh_token": "rotated", "expires_in": 3600}

    def mutation():
        if operation == "login":
            token_store.write_tokens(
                tmp_path, token_store.OAuthTokens("new-login", "new-family", None, 1e12)
            )
        elif operation == "logout":
            token_store.clear_tokens(tmp_path)
        elif operation == "disable":
            manage.disable(tmp_path)
        else:
            manage.set_preference(tmp_path, "api_key")
        finished.set()

    with ThreadPoolExecutor(2) as pool:
        refresh = pool.submit(credentials.resolve, tmp_path, post=post)
        assert entered.wait(3)
        update = pool.submit(mutation)
        try:
            assert not finished.wait(0.1)
        finally:
            release.set()
        assert refresh.result(3).token == "refreshed"
        update.result(3)
    saved = token_store.read_tokens(tmp_path)
    if operation == "login":
        assert saved.access_token == "new-login" and saved.refresh_token == "new-family"
    elif operation == "logout":
        assert saved is None and store.read_config(tmp_path).active
    elif operation == "disable":
        assert not store.read_config(tmp_path).active
    else:
        assert store.read_config(tmp_path).preference == "api_key"


def test_distinct_profiles_progress_while_one_refresh_is_blocked(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    own_login(first, expiry=1)
    own_login(second, expiry=1)
    entered, release = threading.Event(), threading.Event()

    def blocked(*_):
        entered.set()
        assert release.wait(5)
        return 200, {"access_token": "first-fresh", "expires_in": 3600}

    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(credentials.resolve, first, post=blocked)
        assert entered.wait(3)
        try:
            other = pool.submit(
                credentials.resolve,
                second,
                post=lambda *_: (200, {"access_token": "second-fresh", "expires_in": 3600}),
            )
            assert other.result(3).token == "second-fresh"
        finally:
            release.set()
        assert pending.result(3).token == "first-fresh"


def test_lock_timeout_and_os_error_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(persistence, "_LOCK_TIMEOUT", 0)
    with persistence.profile_transaction(tmp_path):
        with pytest.raises(SubscriptionError, match="busy"):
            token_store.clear_tokens(tmp_path)

    def refused(*_):
        raise OSError(errno.EIO, "PRIVATE-LOCK-CANARY")

    monkeypatch.setattr(persistence, "_try_lock", refused)
    with pytest.raises(SubscriptionError) as caught:
        manage.disable(tmp_path)
    assert "CANARY" not in "".join(traceback.format_exception(caught.value))


def test_unique_restricted_temps_and_atomic_config_merge(tmp_path, monkeypatch):
    paths = []
    replace = persistence.os.replace

    def observed(source, destination):
        path = Path(source)
        paths.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())
        replace(source, destination)

    monkeypatch.setattr(persistence.os, "replace", observed)
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {
                "other": {"untouched": True},
                "openai": {"future-field": "kept"},
            }
        )
    )
    own_login(tmp_path)
    manage.set_preference(tmp_path, "subscription")
    manage.disable(tmp_path)
    assert len(paths) == len(set(paths)) == 4
    assert not any(path.exists() for path in paths)
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["other"] == {"untouched": True}
    assert saved["openai"]["future-field"] == "kept"
    assert saved["openai"]["preference"] == "auto"
    assert stat.S_IMODE((tmp_path / "oauth.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_remote_rotation_then_failed_commit_never_sends_model_or_leaks(
    tmp_path, monkeypatch, failure
):
    sdk = sdk_transport(monkeypatch, tmp_path)
    from lohra.subscription.provider import build_subscription_client

    clock = [1000]
    monkeypatch.setattr(credentials.time, "time", lambda: clock[0])
    own_login(tmp_path)
    client = build_subscription_client(tmp_path)
    original = token_store.token_path(tmp_path).read_bytes()
    posts = []

    def post(*_):
        posts.append(True)
        return 200, {"access_token": jwt(10000), "refresh_token": "rotated", "expires_in": 3600}

    monkeypatch.setattr(oauth, "default_post", post)

    def failed(*_):
        raise OSError("PRIVATE-STORE-CANARY")

    monkeypatch.setattr(persistence.os, failure, failed)
    clock[0] = 2100
    try:
        with pytest.raises(SubscriptionError) as caught:
            client.create(model="synthetic", input="never sent")
        assert "CANARY" not in "".join(traceback.format_exception(caught.value))
        assert len(posts) == 1 and sdk.requests == []
        assert token_store.token_path(tmp_path).read_bytes() == original
        assert list(tmp_path.glob(".*.tmp")) == []
    finally:
        client.close()


@pytest.mark.parametrize("bad", ["nan", "inf", "broken", True, {}, None, 10**400])
def test_invalid_expiry_is_refused_locally(tmp_path, bad):
    own_login(tmp_path)
    token_store.token_path(tmp_path).write_text(
        json.dumps(
            {
                "access_token": "PRIVATE-TOKEN-CANARY",
                "refresh_token": "family",
                "expires_at": bad,
            }
        )
    )
    with pytest.raises(SubscriptionError) as caught:
        credentials.resolve(tmp_path)
    assert "CANARY" not in "".join(traceback.format_exception(caught.value))
    assert token_store.read_tokens(tmp_path) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_token", "private\r\nheader"),
        ("access_token", "private "),
        ("id_token", jwt(10000, " private")),
        ("expires_in", float("nan")),
        ("expires_in", float("inf")),
        ("expires_in", 10**400),
        ("expires_in", -1),
        ("expires_in", "private"),
        ("id_token", jwt(10000, {"invalid": "private"})),
    ],
)
def test_bad_refresh_response_has_only_token_free_error(field, value):
    body = {"access_token": "valid", "expires_in": 3600, field: value}
    family = "private-family"
    with pytest.raises(oauth.OAuthError) as caught:
        oauth.refresh_tokens(family, lambda *_: (200, body))
    assert "private" not in "".join(traceback.format_exception(caught.value))


def test_cli_reports_failed_store_without_false_success(tmp_path, monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)

    def refused(*_):
        raise OSError(errno.EIO, "PRIVATE-CLI-CANARY")

    monkeypatch.setattr(persistence, "_try_lock", refused)
    assert cli.run_auth("disable") == 1
    captured = capsys.readouterr()
    assert "CANARY" not in captured.err and "disabled" not in captured.out


def test_canonical_profile_alias_uses_the_same_lock(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(persistence, "_LOCK_TIMEOUT", 0)
    with persistence.profile_transaction(actual):
        with pytest.raises(SubscriptionError, match="busy"):
            manage.enable(alias)
    manage.enable(alias)
    assert store.read_config(actual).active


def test_config_read_modify_write_reloads_after_waiting(tmp_path, monkeypatch):
    store.write_config(tmp_path, store.SubscriptionConfig("api_key", False))
    entered, release = threading.Event(), threading.Event()
    write = store.atomic_write

    def blocked(path, content):
        if threading.current_thread().name.endswith("_0"):
            entered.set()
            assert release.wait(5)
        write(path, content)

    monkeypatch.setattr(store, "atomic_write", blocked)
    with ThreadPoolExecutor(2) as pool:
        enable = pool.submit(manage.enable, tmp_path)
        assert entered.wait(3)
        prefer = pool.submit(manage.set_preference, tmp_path, "subscription")
        release.set()
        enable.result(3)
        prefer.result(3)
    config = store.read_config(tmp_path)
    assert config.active and config.preference == "subscription"


def test_oversized_rotated_family_cannot_report_a_persisted_success(tmp_path):
    own_login(tmp_path, expiry=1)
    before = token_store.token_path(tmp_path).read_bytes()
    with pytest.raises(SubscriptionError):
        credentials.resolve(
            tmp_path,
            post=lambda *_: (
                200,
                {
                    "access_token": "x" * 64000,
                    "refresh_token": "rotated",
                    "expires_in": 3600,
                },
            ),
        )
    assert token_store.token_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("source", ["own", "codex"])
def test_symlinked_auth_file_is_not_followed(tmp_path, monkeypatch, source):
    from lohra.subscription.provider import build_subscription_client
    from tests.subscription_fakes import codex_login

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    own_login(tmp_path, expiry=1e12)
    target = tmp_path / "target.json"
    if source == "own":
        path = token_store.token_path(tmp_path)
        path.rename(target)
    else:
        token_store.clear_tokens(tmp_path)
        codex_login(target, expiry=int(time.time()) + 3600)
        path = tmp_path / "codex" / "auth.json"
        path.parent.mkdir()
    path.symlink_to(target)
    with pytest.raises(SubscriptionError):
        build_subscription_client(tmp_path)
    assert path.is_symlink() and target.exists()


@pytest.mark.parametrize("source", ["own", "codex", "config"])
def test_deeply_malformed_auth_json_is_a_local_refusal(tmp_path, monkeypatch, source):
    from lohra.subscription.provider import build_subscription_client
    from tests.subscription_fakes import codex_login

    own_login(tmp_path, expiry=1e12)
    codex = tmp_path / "codex" / "auth.json"
    codex_login(codex, expiry=int(time.time()) + 3600)
    monkeypatch.setenv("CODEX_HOME", str(codex.parent))
    if source == "codex":
        token_store.clear_tokens(tmp_path)
    path = {
        "own": token_store.token_path(tmp_path),
        "codex": codex,
        "config": store.auth_path(tmp_path),
    }[source]
    path.write_text("[" * 2000 + "]" * 2000)
    with pytest.raises(SubscriptionError):
        build_subscription_client(tmp_path)


def test_codex_jwt_claim_parse_failure_is_a_local_refusal(tmp_path, monkeypatch):
    import base64
    from lohra.subscription.provider import build_subscription_client
    from tests.subscription_fakes import codex_login

    codex = tmp_path / "codex" / "auth.json"
    codex_login(codex)
    payload = base64.urlsafe_b64encode(("[" * 2000 + "]" * 2000).encode()).decode()
    codex.write_text(json.dumps({"tokens": {"access_token": f"synthetic.{payload}.signature"}}))
    monkeypatch.setenv("CODEX_HOME", str(codex.parent))
    store.write_config(tmp_path, store.SubscriptionConfig("subscription", True))
    with pytest.raises(SubscriptionError):
        build_subscription_client(tmp_path)
