"""Actual file stores and native locks; all credentials and refreshes are synthetic."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
import threading
import time

from lohra.subscription import credentials, persistence, store, token_store
from tests.subscription_fakes import jwt, own_login


def _resolve_process(home, started, release, count, results, contended):
    acquire = persistence._try_lock

    def observed(fd):
        try:
            acquire(fd)
        except OSError:
            contended.set()
            raise

    persistence._try_lock = observed

    def post(url, body):
        with count.get_lock():
            count.value += 1
        started.set()
        assert release.wait(5)
        return 200, {
            "access_token": jwt(time.time() + 3600),
            "refresh_token": "rotated-family",
            "expires_in": 3600,
        }

    try:
        result = credentials.resolve(Path(home), post=post)
        results.put((result.token, token_store.read_tokens(Path(home)).refresh_token))
    except Exception as exc:
        results.put(type(exc).__name__)


def test_threads_refresh_one_family_once(tmp_path, monkeypatch):
    own_login(tmp_path, expiry=1)
    entered = threading.Event()
    release = threading.Event()
    count = []
    contended = threading.Event()
    acquire = persistence._try_lock

    def observed(fd):
        try:
            acquire(fd)
        except OSError:
            contended.set()
            raise

    monkeypatch.setattr(persistence, "_try_lock", observed)

    def post(url, body):
        count.append(body["refresh_token"])
        entered.set()
        assert release.wait(5)
        return 200, {
            "access_token": jwt(time.time() + 3600),
            "refresh_token": "rotated-family",
            "expires_in": 3600,
        }

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(credentials.resolve, tmp_path, post=post)
        assert entered.wait(3)
        second = pool.submit(credentials.resolve, tmp_path, post=post)
        assert contended.wait(3)
        release.set()
        snapshots = [first.result(5), second.result(5)]
    assert count == ["synthetic-family"]
    assert snapshots[0] == snapshots[1]
    assert token_store.read_tokens(tmp_path).refresh_token == "rotated-family"


def test_processes_refresh_one_family_once(tmp_path):
    own_login(tmp_path, expiry=1)
    ctx = multiprocessing.get_context("spawn")
    started, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    count = ctx.Value("i", 0)
    contended = ctx.Event()
    processes = [
        ctx.Process(
            target=_resolve_process,
            args=(str(tmp_path), started, release, count, results, contended),
        )
        for _ in range(2)
    ]
    try:
        processes[0].start()
        assert started.wait(5)
        processes[1].start()
        assert contended.wait(5)
        release.set()
        snapshots = [results.get(timeout=8), results.get(timeout=8)]
        for process in processes:
            process.join(5)
            assert process.exitcode == 0
        assert count.value == 1
        assert snapshots[0] == snapshots[1]
        assert snapshots[0][1] == "rotated-family"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)


def test_invalid_own_store_never_switches_to_codex_account(tmp_path, monkeypatch):
    from tests.subscription_fakes import codex_login

    own_login(tmp_path)
    codex = tmp_path / "codex"
    codex.mkdir()
    codex_login(codex / "auth.json", expiry=time.time() + 3600)
    monkeypatch.setenv("CODEX_HOME", str(codex))
    token_store.token_path(tmp_path).write_text('{"access_token":"private", "expires_at":"nan"}')
    import pytest

    with pytest.raises(credentials.SubscriptionError):
        credentials.resolve(tmp_path)
    assert store.read_config(tmp_path).active
