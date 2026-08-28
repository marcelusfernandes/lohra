"""The auth preference must reach EVERY consumer that answers "which path will
chat take?" — not just chat itself (model-routing, slice A, review round 2).

`lohra doctor` promises in its own docstrings that it can "never contradict the
chat path". A preference read only by `run_chat`/`build_dashboard_app` breaks
that promise: doctor reports a green subscription while chat dies on the API-key
path, and its remedy (`lohra auth login`) fixes a route nobody takes.

The split enforced here:

* **route-aware** (what WILL happen): doctor, the snapshot's `usable` /
  `subscription_divergence`, `wizard.evaluate`, the profile cost warning, and
  the cross-provider escalation gate;
* **opt-in-aware** (what is ON FILE): the `lohra serve` refusal and
  `manage.status()["active"]` — security gates, not preferences.
"""

from __future__ import annotations

import io
import json
import shlex

import pytest

from lohra import cli
from lohra.onboarding import choice, detect, doctor, wizard
from lohra.subscription import manage, store
from lohra.subscription.credentials import route_for


# --- helpers -----------------------------------------------------------------


def _write_auth(home, **entry) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"openai": entry}), encoding="utf-8")


def _active(**extra) -> dict:
    return {"auth_mode": "subscription", "acknowledged_tos_risk": True, **extra}


def _inactive(**extra) -> dict:
    return {"auth_mode": "api_key", "acknowledged_tos_risk": False, **extra}


def _login(home) -> None:
    """A usable subscription needs a login too, not just the opt-in record."""
    from lohra.subscription import token_store

    token_store.write_tokens(
        home,
        token_store.OAuthTokens(
            access_token="a", refresh_token="r", account_id=None, expires_at=9e9
        ),
    )


def _dead():
    return detect.OllamaStatus(alive=False, url=detect.OLLAMA_TAGS_URL, detail="stub")


def _snapshot(home, *, env=None):
    return detect.detect_environment(
        env=dict(env or {}),
        base=home,
        user_home=home / "user",
        ollama_probe=_dead,
        which=lambda name: None,
        stdin=io.StringIO(),
        stderr=io.StringIO(),
        version_info=(3, 12, 3),
    )


def _by_name(checks):
    return {check.name: check for check in checks}


# --- the truth table, extracted once so nobody can re-implement it ------------


@pytest.mark.parametrize(
    ("preference", "active", "mode", "fails"),
    [
        ("auto", True, "subscription", False),
        ("auto", False, "api_key", False),
        ("api_key", True, "api_key", False),
        ("api_key", False, "api_key", False),
        ("subscription", True, "subscription", False),
        ("subscription", False, "api_key", True),
    ],
)
def test_route_for_is_the_single_truth_table(preference, active, mode, fails):
    route = route_for(preference, active)
    assert route.mode == mode
    assert (route.error is not None) is fails


def test_resolve_auth_route_is_route_for_over_the_stored_config(tmp_path):
    """The disk reader and the pure table must not drift apart."""
    from lohra.subscription.credentials import resolve_auth_route

    _write_auth(tmp_path, **_active(preference="api_key"))
    assert resolve_auth_route(tmp_path) == route_for("api_key", True)


# --- the snapshot tells the truth about the route ----------------------------


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (_active(), "subscription"),
        (_active(preference="auto"), "subscription"),
        (_active(preference="api_key"), "api_key"),
        (_active(preference="subscription"), "subscription"),
        (_inactive(), "api_key"),
        (_inactive(preference="api_key"), "api_key"),
        (_inactive(preference="subscription"), "unusable"),
    ],
)
def test_snapshot_auth_route_follows_the_preference(tmp_path, entry, expected):
    _write_auth(tmp_path, **entry)
    snap = _snapshot(tmp_path)
    assert snap.auth_route == expected
    assert snap.auth_preference == entry.get("preference", "auto")


def test_usable_is_false_when_the_preferred_key_path_has_no_key(tmp_path):
    """subscription_active is True, but chat will die on the key path — say so."""
    _write_auth(tmp_path, **_active(preference="api_key"))
    snap = _snapshot(tmp_path)
    assert snap.subscription_active is True  # the opt-in IS on file
    assert snap.usable is False


def test_usable_is_false_when_the_preferred_subscription_is_unusable(tmp_path):
    """Even a perfectly good API key cannot answer: chat exits 2 first."""
    _write_auth(tmp_path, **_inactive(preference="subscription"))
    snap = _snapshot(tmp_path, env={"ANTHROPIC_API_KEY": "sk-x"})
    assert snap.has_api_key is True
    assert snap.usable is False


def test_subscription_divergence_is_quiet_when_the_base_prefers_the_key_path(tmp_path):
    """No divergence to report: the shared home bills the paid key too."""
    _write_auth(tmp_path, **_active(preference="api_key"))
    (tmp_path / "profiles" / "work").mkdir(parents=True, exist_ok=True)
    snap = _snapshot(tmp_path, env={"LOHRA_PROFILE": "work"})
    assert snap.subscription_divergence is False


# --- doctor cannot contradict chat -------------------------------------------


def test_doctor_fails_when_the_key_path_is_preferred_but_has_no_key(tmp_path):
    _write_auth(tmp_path, **_active(preference="api_key"))
    _login(tmp_path)
    checks = doctor.run_checks(_snapshot(tmp_path), env={})

    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.FAIL  # chat exits 2 here — doctor must agree
    assert doctor.exit_code(checks) == 2


def test_doctor_reports_the_key_provider_under_preference_api_key(tmp_path):
    _write_auth(tmp_path, **_active(preference="api_key"))
    _login(tmp_path)
    checks = doctor.run_checks(_snapshot(tmp_path, env={"ANTHROPIC_API_KEY": "sk-x"}),
                               env={"ANTHROPIC_API_KEY": "sk-x"})

    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.OK and "anthropic" in provider.detail
    assert doctor.exit_code(checks) == 0


def test_doctor_subscription_line_stays_ok_and_names_the_preference(tmp_path):
    """The user CHOSE the key path — a warn here would cry wolf."""
    _write_auth(tmp_path, **_active(preference="api_key"))
    _login(tmp_path)
    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))["subscription"]

    assert check.state == doctor.OK
    assert "preference=api_key" in check.detail


def test_doctor_fails_with_a_copyable_remedy_on_an_unusable_preference(tmp_path):
    _write_auth(tmp_path, **_inactive(preference="subscription"))
    checks = doctor.run_checks(_snapshot(tmp_path, env={"ANTHROPIC_API_KEY": "sk-x"}),
                               env={"ANTHROPIC_API_KEY": "sk-x"})

    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.FAIL
    assert "prefer" in provider.remedy
    # ONB-6 contract: the remedy is a command that actually parses.
    command = provider.remedy.split("#")[0].strip()
    cli.build_parser().parse_args(shlex.split(command)[1:])
    assert doctor.exit_code(checks) == 2


def test_wizard_evaluate_is_not_ready_when_the_key_path_is_preferred(tmp_path):
    _write_auth(tmp_path, **_active(preference="api_key"))
    snap = _snapshot(tmp_path)
    ready, message = wizard.evaluate(snap, {})
    assert ready is False and message


# --- the profile cost warning must not cry wolf ------------------------------


def test_cost_warning_is_silent_when_the_base_prefers_the_key_path(tmp_path):
    base = tmp_path / "base"
    home = base / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    _write_auth(base, **_active(preference="api_key"))

    assert choice.cost_warning(base=base, home=home, profile="work") is None


def test_cost_warning_still_fires_on_a_real_divergence(tmp_path):
    base = tmp_path / "base"
    home = base / "profiles" / "work"
    home.mkdir(parents=True, exist_ok=True)
    _write_auth(base, **_active())

    assert "work" in (choice.cost_warning(base=base, home=home, profile="work") or "")


# --- following the error's own remedy keeps the choice -----------------------


def test_enable_preserves_an_explicit_subscription_preference(tmp_path):
    """`auth enable` is exactly what the preference=subscription error tells you
    to run; wiping the preference there would re-arm the silent paid fallback."""
    manage.set_preference(tmp_path, "subscription")
    manage.enable(tmp_path)
    assert store.read_config(tmp_path).preference == "subscription"


def test_enable_clears_a_stale_api_key_preference(tmp_path):
    """Otherwise `auth enable` would be a no-op the user cannot see."""
    manage.set_preference(tmp_path, "api_key")
    manage.enable(tmp_path)
    assert store.read_config(tmp_path).preference == "auto"


def test_disable_clears_a_stale_subscription_preference(tmp_path):
    """A preference=subscription surviving a disable leaves chat erroring out."""
    manage.enable(tmp_path)
    manage.set_preference(tmp_path, "subscription")
    manage.disable(tmp_path)
    assert store.read_config(tmp_path).preference == "auto"


def test_disable_preserves_an_api_key_preference(tmp_path):
    manage.enable(tmp_path)
    manage.set_preference(tmp_path, "api_key")
    manage.disable(tmp_path)
    assert store.read_config(tmp_path).preference == "api_key"


def test_login_reaches_enable_and_keeps_the_subscription_preference(tmp_path):
    from lohra.onboarding import auth_login

    manage.set_preference(tmp_path, "subscription")
    auth_login._default_enable(tmp_path)
    assert store.read_config(tmp_path).preference == "subscription"


# --- `lohra auth <action>` must not swallow a stray argument -----------------


@pytest.mark.parametrize("action", ["status", "enable", "disable", "login", "logout"])
def test_auth_actions_reject_a_stray_positional(tmp_path, monkeypatch, capsys, action):
    """`lohra auth disable subscription` used to be an argparse error. Silently
    accepting it DISABLES the subscription of a user who meant `prefer`."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    manage.enable(tmp_path)

    assert cli.run_auth(action, value="subscription", assume_yes=True, no_input=True) == 2
    err = capsys.readouterr().err
    assert "prefer" in err
    assert store.read_config(tmp_path).active is True  # nothing was changed


def test_auth_prefer_still_takes_its_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    assert cli.run_auth("prefer", value="api_key") == 0
    assert store.read_config(tmp_path).preference == "api_key"


# --- cross-provider escalation honours the preference ------------------------


class _FakeClient:
    def close(self):  # pragma: no cover - defensive
        pass


class _Parent:
    name = "anthropic"


def test_a_leaf_cannot_escalate_to_the_subscription_under_preference_api_key(tmp_path):
    """Workflow specs are agent-authored: `provider: openai-codex` in a node is
    not the human's voice. The stored preference is."""
    from lohra.agent.client_pool import ClientPool, ProviderError

    _write_auth(tmp_path, **_active(preference="api_key"))
    pool = ClientPool(parent_provider=_Parent(), parent_client=_FakeClient(), home=tmp_path)

    with pytest.raises(ProviderError) as excinfo:
        pool.get("openai-codex")
    assert "prefer" in str(excinfo.value)


def test_escalation_still_refused_without_the_opt_in(tmp_path):
    from lohra.agent.client_pool import ClientPool, ProviderError

    pool = ClientPool(parent_provider=_Parent(), parent_client=_FakeClient(), home=tmp_path)
    with pytest.raises(ProviderError) as excinfo:
        pool.get("openai-codex")
    assert "lohra auth enable" in str(excinfo.value)


# --- the serve gate stays unconditional, but says how to get past it ---------


def test_serve_refusal_names_the_command_that_actually_unblocks_it(tmp_path, monkeypatch, capsys):
    """`auth prefer api_key` does NOT unblock serve (the gate is unconditional);
    a user who just ran it must not be sent in a circle."""
    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)
    _write_auth(tmp_path, **_active(preference="api_key"))

    assert cli.run_openai_server(host="127.0.0.1", port=0) == 2
    err = capsys.readouterr().err
    assert "lohra auth disable" in err and "prefer" in err
