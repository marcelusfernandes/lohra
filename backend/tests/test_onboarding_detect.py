"""Environment detection for onboarding (ONB-2).

Every probe here is injected: no real network, no real ``$HOME``, no sleep. The
four machines from the aceite (virgin / two keys / Codex-only / Ollama-only) are
built out of a plain dict + ``tmp_path``, so the snapshot is a pure function of
its inputs.
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
from pathlib import Path

import httpx
import pytest

from lohra.onboarding import detect


# --- helpers -----------------------------------------------------------------


class _FakeTTY(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _RudeStream(io.StringIO):
    def isatty(self):  # a wrapper that raises instead of answering
        raise ValueError("detached")


class _DeadProbe:
    """An Ollama probe that always reports 'dead' and counts its calls.

    Counting is the guard against a regression to a real network probe: a test
    that leaves this at 0 calls would be silently hitting localhost.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> detect.OllamaStatus:
        self.calls += 1
        return detect.OllamaStatus(alive=False, url=detect.OLLAMA_TAGS_URL, detail="stub")


def _snapshot(tmp_path: Path, **kwargs):
    """detect_environment with every ambient source pinned to tmp_path."""
    probe = kwargs.pop("ollama_probe", None) or _DeadProbe()
    params = dict(
        env={},
        base=tmp_path / "lohra",
        user_home=tmp_path / "user",
        ollama_probe=probe,
        which=lambda name: None,
        stdin=_FakeTTY(False),
        stderr=_FakeTTY(False),
    )
    params.update(kwargs)
    return detect.detect_environment(**params)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- the four machines from the aceite ---------------------------------------


def test_virgin_machine_detects_nothing_and_says_so(tmp_path):
    probe = _DeadProbe()
    snap = _snapshot(tmp_path, ollama_probe=probe)

    assert probe.calls == 1  # the probe ran, and it was the injected one
    assert snap.detected_provider is None
    assert snap.provider_origin == "none"
    assert snap.has_api_key is False
    assert snap.subscription_active is False
    assert snap.base_subscription_active is False
    assert snap.codex_auth_present is False
    assert snap.lohra_auth_present is False
    assert snap.lohra_oauth_present is False
    assert snap.env_file_present is False
    assert snap.ollama.alive is False
    assert snap.usable is False
    assert snap.active_profile is None
    assert [p.provider for p in snap.providers if p.configured] == []


def test_two_keys_registration_order_decides_and_both_are_reported(tmp_path):
    snap = _snapshot(
        tmp_path,
        env={"ANTHROPIC_API_KEY": "sk-a", "OPENAI_API_KEY": "sk-o"},
    )

    # anthropic is first in BUILTIN_PROFILES, so auto-detection picks it.
    assert snap.detected_provider == "anthropic"
    assert snap.provider_origin == "api-key"
    assert snap.has_api_key is True
    assert snap.usable is True
    configured = {p.provider: p.present_vars for p in snap.providers if p.configured}
    assert configured == {"anthropic": ("ANTHROPIC_API_KEY",), "openai": ("OPENAI_API_KEY",)}


def test_explicit_lohra_provider_beats_key_detection(tmp_path):
    snap = _snapshot(tmp_path, env={"ANTHROPIC_API_KEY": "sk-a", "LOHRA_PROVIDER": "groq"})
    assert snap.detected_provider == "groq"
    assert snap.provider_origin == "env-var"


def test_an_empty_key_counts_as_unset(tmp_path):
    snap = _snapshot(tmp_path, env={"ANTHROPIC_API_KEY": ""})
    assert snap.detected_provider is None
    assert snap.has_api_key is False


def test_key_presence_mirrors_the_resolver_rule(tmp_path):
    # The resolver's key scan is plain truthiness, so a whitespace-only value
    # DOES select the provider. Detection must predict that, not disagree with
    # it — reporting "no key" here while chat picks anthropic would be a lie.
    from lohra.providers import resolve_provider_name

    env = {"ANTHROPIC_API_KEY": "   "}
    assert resolve_provider_name(env=env) == "anthropic"
    snap = _snapshot(tmp_path, env=env)
    assert snap.detected_provider == "anthropic"
    assert snap.has_api_key is True


def test_a_typo_in_lohra_provider_is_reported_not_raised(tmp_path):
    snap = _snapshot(tmp_path, env={"LOHRA_PROVIDER": "antropic"})
    assert snap.detected_provider is None
    assert snap.provider_origin == "none"
    assert snap.provider_error and "unknown provider" in snap.provider_error


def test_codex_only_machine_sees_the_login_but_not_an_active_subscription(tmp_path):
    codex_home = tmp_path / "codexhome"
    _write(codex_home / "auth.json", json.dumps({"tokens": {"access_token": "tok"}}))

    snap = _snapshot(tmp_path, env={"CODEX_HOME": str(codex_home)})

    assert snap.codex_home == str(codex_home)
    assert snap.codex_auth_present is True
    # A Codex login is NOT consent: subscription mode stays off until opt-in.
    assert snap.subscription_active is False
    assert snap.has_api_key is False
    assert snap.usable is False


def test_ollama_only_machine_is_usable_without_any_key(tmp_path):
    alive = detect.OllamaStatus(
        alive=True, url=detect.OLLAMA_TAGS_URL, models=("llama3.2", "qwen2.5")
    )
    snap = _snapshot(tmp_path, ollama_probe=lambda: alive)

    assert snap.ollama.alive is True
    assert snap.ollama.models == ("llama3.2", "qwen2.5")
    assert snap.has_api_key is False
    assert snap.detected_provider is None  # keyless: auto-detection can't see it
    assert snap.usable is True


# --- subscription / profile stores -------------------------------------------


def _opt_in(home: Path) -> None:
    _write(
        home / "auth.json",
        json.dumps({"openai": {"auth_mode": "subscription", "acknowledged_tos_risk": True}}),
    )


def test_subscription_opt_in_is_detected_in_the_active_home(tmp_path):
    base = tmp_path / "lohra"
    _opt_in(base)
    snap = _snapshot(tmp_path, base=base)

    assert snap.lohra_auth_present is True
    assert snap.subscription_active is True
    assert snap.base_subscription_active is True
    assert snap.usable is True


def test_profile_without_subscription_diverges_from_a_subscribed_base(tmp_path):
    # The ONB-9 cost footgun: the base opted in, the profile store did not.
    base = tmp_path / "lohra"
    _opt_in(base)
    snap = _snapshot(tmp_path, base=base, env={"LOHRA_PROFILE": "work"})

    assert snap.active_profile == "work"
    assert snap.home == str(base / "profiles" / "work")
    assert snap.subscription_active is False
    assert snap.base_subscription_active is True
    assert snap.subscription_divergence is True


def test_no_divergence_without_a_profile(tmp_path):
    base = tmp_path / "lohra"
    _opt_in(base)
    snap = _snapshot(tmp_path, base=base)
    assert snap.subscription_divergence is False


def test_invalid_profile_name_degrades_to_no_profile(tmp_path):
    snap = _snapshot(tmp_path, env={"LOHRA_PROFILE": "../escape"})
    assert snap.active_profile is None
    assert snap.home == snap.base


def test_lohra_own_oauth_login_is_detected(tmp_path):
    base = tmp_path / "lohra"
    _write(
        base / "oauth.json",
        json.dumps({"access_token": "tok", "refresh_token": "r", "expires_at": 1234.0}),
    )
    snap = _snapshot(tmp_path, base=base)
    assert snap.lohra_oauth_present is True
    assert snap.lohra_oauth_expires_at == 1234.0


def test_malformed_credential_files_never_raise(tmp_path):
    base = tmp_path / "lohra"
    _write(base / "auth.json", "{not json at all")
    _write(base / "oauth.json", "]]]")
    codex_home = tmp_path / "codexhome"
    _write(codex_home / "auth.json", "nope")

    snap = _snapshot(tmp_path, base=base, env={"CODEX_HOME": str(codex_home)})

    assert snap.subscription_active is False
    assert snap.lohra_oauth_present is False
    assert snap.lohra_auth_present is True  # the file exists; it just doesn't parse
    assert snap.codex_auth_present is True


# --- .env, harnesses, python, tty --------------------------------------------


def test_env_file_is_read_from_the_base_even_under_a_profile(tmp_path):
    base = tmp_path / "lohra"
    _write(base / ".env", "ANTHROPIC_API_KEY=sk-x\n")
    snap = _snapshot(tmp_path, base=base, env={"LOHRA_PROFILE": "work"})

    # .env is global by design (lohra_base, never lohra_home) — see cli.main.
    assert snap.env_file == str(base / ".env")
    assert snap.env_file_present is True


def test_harnesses_report_binary_and_home(tmp_path):
    user_home = tmp_path / "user"
    (user_home / ".claude").mkdir(parents=True)
    codex_home = tmp_path / "codexhome"

    snap = _snapshot(
        tmp_path,
        user_home=user_home,
        env={"CODEX_HOME": str(codex_home)},
        which=lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )

    found = {h.name: h for h in snap.harnesses}
    assert set(found) == {"claude", "codex"}
    assert found["claude"].path == "/usr/local/bin/claude"
    assert found["claude"].installed is True
    assert found["claude"].home_present is True
    assert found["codex"].path is None
    assert found["codex"].installed is False
    assert found["codex"].home == str(codex_home)
    assert found["codex"].home_present is False


def test_python_version_range_is_reported(tmp_path):
    ok = _snapshot(tmp_path, version_info=(3, 12, 3))
    assert ok.python_version == "3.12.3"
    assert ok.python_supported is True

    old = _snapshot(tmp_path, version_info=(3, 9, 18))
    assert old.python_supported is False

    too_new = _snapshot(tmp_path, version_info=(3, 14, 0))
    assert too_new.python_supported is False


def test_tty_detection_uses_both_streams(tmp_path):
    both = _snapshot(tmp_path, stdin=_FakeTTY(True), stderr=_FakeTTY(True))
    assert (both.stdin_tty, both.stderr_tty, both.interactive) == (True, True, True)

    piped = _snapshot(tmp_path, stdin=_FakeTTY(False), stderr=_FakeTTY(True))
    assert piped.interactive is False


def test_a_stream_that_raises_on_isatty_counts_as_not_a_tty(tmp_path):
    snap = _snapshot(tmp_path, stdin=_RudeStream(), stderr=_RudeStream())
    assert snap.interactive is False


def test_platform_is_recorded(tmp_path):
    snap = _snapshot(tmp_path)
    assert snap.platform and isinstance(snap.platform, str)
    assert snap.os_name in ("posix", "nt", "java")


# --- shape contract ----------------------------------------------------------


def test_snapshot_is_json_serializable(tmp_path):
    snap = _snapshot(tmp_path, env={"ANTHROPIC_API_KEY": "sk-a"})
    text = json.dumps(snap.to_dict(), ensure_ascii=True)
    back = json.loads(text)
    assert back["detected_provider"] == "anthropic"
    assert back["ollama"]["alive"] is False
    assert isinstance(back["providers"], list)
    assert isinstance(back["harnesses"], list)


def test_snapshot_never_carries_a_secret(tmp_path):
    base = tmp_path / "lohra"
    _write(base / "oauth.json", json.dumps({"access_token": "SUPERSECRET", "expires_at": 1.0}))
    snap = _snapshot(
        tmp_path, base=base, env={"ANTHROPIC_API_KEY": "sk-THIS-IS-THE-KEY"}
    )
    blob = json.dumps(snap.to_dict()) + repr(snap)
    assert "SUPERSECRET" not in blob
    assert "sk-THIS-IS-THE-KEY" not in blob


def test_snapshot_is_immutable(tmp_path):
    snap = _snapshot(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.detected_provider = "openai"  # type: ignore[misc]
    assert isinstance(snap.providers, tuple)


def test_detection_stays_inside_its_time_budget(tmp_path):
    started = time.perf_counter()
    _snapshot(tmp_path)
    assert time.perf_counter() - started < 1.0


# --- the Ollama probe itself (the only networked piece) ----------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_probe_reports_alive_and_lists_models():
    def handler(request):
        assert str(request.url) == detect.OLLAMA_TAGS_URL
        return httpx.Response(200, json={"models": [{"name": "llama3.2"}, {"name": "qwen2.5"}]})

    status = detect.probe_ollama(client=_client(handler))
    assert status.alive is True
    assert status.models == ("llama3.2", "qwen2.5")
    assert status.url == detect.OLLAMA_TAGS_URL


def test_probe_is_alive_even_when_no_model_is_pulled():
    status = detect.probe_ollama(client=_client(lambda r: httpx.Response(200, json={"models": []})))
    assert status.alive is True
    assert status.models == ()


def test_probe_reports_dead_on_connection_refused():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    status = detect.probe_ollama(client=_client(handler))
    assert status.alive is False
    assert status.detail  # a short, human-readable reason


def test_probe_reports_dead_on_timeout():
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    assert detect.probe_ollama(client=_client(handler)).alive is False


def test_probe_reports_dead_on_a_non_200():
    assert detect.probe_ollama(client=_client(lambda r: httpx.Response(500))).alive is False


def test_probe_survives_a_body_that_is_not_ollama():
    status = detect.probe_ollama(client=_client(lambda r: httpx.Response(200, text="<html>")))
    assert status.alive is False


def test_probe_survives_a_json_body_that_is_not_an_object():
    # Something else answering on 11434 must not crash the probe.
    status = detect.probe_ollama(client=_client(lambda r: httpx.Response(200, json=[1, 2])))
    assert status.alive is True
    assert status.models == ()


def test_probe_survives_a_json_body_of_the_wrong_shape():
    status = detect.probe_ollama(client=_client(lambda r: httpx.Response(200, json={"models": 7})))
    assert status.alive is True
    assert status.models == ()


def test_probe_builds_and_closes_its_own_client_when_none_is_given(monkeypatch):
    """The production path (no injected client) must construct httpx correctly."""
    closed = []

    def handler(request):
        return httpx.Response(200, json={"models": [{"name": "phi4"}]})

    class _TrackingClient(httpx.Client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(httpx, "Client", _TrackingClient)
    status = detect.probe_ollama()
    assert status.alive is True and status.models == ("phi4",)
    assert closed == [True]  # no leaked socket


def test_a_broken_path_lookup_does_not_kill_detection(tmp_path):
    def rude_which(name):
        raise OSError("PATH is on fire")

    snap = _snapshot(tmp_path, which=rude_which)
    assert [h.installed for h in snap.harnesses] == [False, False]


def test_snapshot_json_is_one_stable_ascii_line(tmp_path):
    snap = _snapshot(tmp_path)
    line = detect.snapshot_json(snap)
    assert "\n" not in line
    assert line == line.encode("ascii").decode("ascii")
    assert json.loads(line)["usable"] is False


def test_ambient_defaults_work_without_any_injection(monkeypatch, tmp_path):
    """env/base/user_home default to the process state (the production call)."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "lohra"))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    snap = detect.detect_environment(ollama_probe=_DeadProbe())  # no network
    assert snap.base == str(tmp_path / "lohra")
    assert snap.home == snap.base
    json.dumps(snap.to_dict())
