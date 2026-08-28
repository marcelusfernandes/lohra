"""Auth preference (model-routing, slice A): auto | subscription | api_key.

The preference lives in the SAME per-profile ~/.lohra/auth.json as the
subscription opt-in, so it is isolated per profile (the .env is not). Its whole
contract is the truth table exercised here:

  auto (or absent/garbage) -> exactly today's `if subscription_active(...)`
  api_key + active         -> API key, with ONE note on stderr
  subscription + inactive  -> a didactic error naming the remedy, never a
                              silent fallback to the API key
"""

import json

import pytest

from lohra.subscription import manage, store
from lohra.subscription.credentials import resolve_auth_route, subscription_active


def _raw_auth_json(home, entry: dict) -> None:
    """Write auth.json by hand — the only way to test a MISSING preference key
    (write_config always emits one)."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"openai": entry}), encoding="utf-8")


def _active_entry(**extra) -> dict:
    return {"auth_mode": "subscription", "acknowledged_tos_risk": True, **extra}


def _inactive_entry(**extra) -> dict:
    return {"auth_mode": "api_key", "acknowledged_tos_risk": False, **extra}


# --- store: strict read of the closed value set ---


def test_preference_defaults_to_auto_when_the_field_is_absent(tmp_path):
    _raw_auth_json(tmp_path, _active_entry())
    assert store.read_config(tmp_path).preference == "auto"


@pytest.mark.parametrize("garbage", ["aggressive", "AUTO", "", 1, True, None, [], {"a": 1}])
def test_preference_reads_strictly_against_the_closed_set(tmp_path, garbage):
    _raw_auth_json(tmp_path, _active_entry(preference=garbage))
    assert store.read_config(tmp_path).preference == "auto"


@pytest.mark.parametrize("value", ["auto", "subscription", "api_key"])
def test_preference_roundtrips_and_preserves_the_tos_acknowledgement(tmp_path, value):
    manage.enable(tmp_path)  # acknowledged_tos_risk = True
    manage.set_preference(tmp_path, value)
    cfg = store.read_config(tmp_path)
    assert cfg.preference == value
    assert cfg.acknowledged_tos_risk is True and cfg.auth_mode == "subscription"


def test_set_preference_on_a_virgin_home_writes_the_default_off_config(tmp_path):
    manage.set_preference(tmp_path, "api_key")
    cfg = store.read_config(tmp_path)
    assert cfg.preference == "api_key"
    assert cfg.active is False and cfg.acknowledged_tos_risk is False


def test_write_config_preserves_unknown_keys_in_the_entry(tmp_path):
    _raw_auth_json(tmp_path, _active_entry(future_field="keep-me"))
    store.write_config(tmp_path, store.read_config(tmp_path))
    data = json.loads((tmp_path / "auth.json").read_text())
    assert data["openai"]["future_field"] == "keep-me"


def test_enable_and_disable_reset_the_preference_to_auto(tmp_path):
    # Deliberate: `auth enable`/`auth disable` are the explicit mode switches, so
    # they clear the finer-grained override (otherwise `disable` + a stale
    # preference="subscription" would leave chat permanently erroring out).
    manage.set_preference(tmp_path, "api_key")
    manage.enable(tmp_path)
    assert store.read_config(tmp_path).preference == "auto"
    manage.set_preference(tmp_path, "subscription")
    manage.disable(tmp_path)
    assert store.read_config(tmp_path).preference == "auto"


# --- resolve_auth_route: the truth table ---


def test_no_auth_json_at_all_routes_to_the_api_key(tmp_path):
    route = resolve_auth_route(tmp_path)
    assert route.mode == "api_key" and route.note is None and route.error is None


@pytest.mark.parametrize("entry", [_active_entry(), _inactive_entry()])
def test_without_the_field_the_route_equals_todays_subscription_active(tmp_path, entry):
    """Byte-compat: no preference stored -> exactly `if subscription_active(...)`."""
    _raw_auth_json(tmp_path, entry)
    route = resolve_auth_route(tmp_path)
    expected = "subscription" if subscription_active(tmp_path) else "api_key"
    assert route.mode == expected
    assert route.note is None and route.error is None


@pytest.mark.parametrize("entry", [_active_entry(), _inactive_entry()])
def test_preference_auto_equals_todays_subscription_active(tmp_path, entry):
    _raw_auth_json(tmp_path, dict(entry, preference="auto"))
    route = resolve_auth_route(tmp_path)
    assert route.mode == ("subscription" if subscription_active(tmp_path) else "api_key")
    assert route.note is None and route.error is None


def test_preference_api_key_with_an_active_subscription_routes_to_key_with_a_note(tmp_path):
    _raw_auth_json(tmp_path, _active_entry(preference="api_key"))
    assert subscription_active(tmp_path) is True  # the subscription IS usable
    route = resolve_auth_route(tmp_path)
    assert route.mode == "api_key" and route.error is None
    assert route.note and "preference=api_key" in route.note


def test_preference_api_key_without_a_subscription_is_silent(tmp_path):
    _raw_auth_json(tmp_path, _inactive_entry(preference="api_key"))
    route = resolve_auth_route(tmp_path)
    assert route.mode == "api_key" and route.note is None and route.error is None


def test_preference_subscription_with_an_active_subscription_routes_to_it(tmp_path):
    _raw_auth_json(tmp_path, _active_entry(preference="subscription"))
    route = resolve_auth_route(tmp_path)
    assert route.mode == "subscription" and route.note is None and route.error is None


@pytest.mark.parametrize(
    "entry",
    [
        _inactive_entry(preference="subscription"),  # never enabled
        # enabled but the ToS risk was never acknowledged -> not usable either
        {"auth_mode": "subscription", "acknowledged_tos_risk": False, "preference": "subscription"},
    ],
)
def test_preference_subscription_without_one_is_a_didactic_error(tmp_path, entry):
    _raw_auth_json(tmp_path, entry)
    route = resolve_auth_route(tmp_path)
    assert route.error is not None  # NEVER a silent fallback to the API key
    assert "lohra auth enable" in route.error and "lohra auth login" in route.error


def test_the_route_is_immutable(tmp_path):
    route = resolve_auth_route(tmp_path)
    with pytest.raises(Exception):
        route.mode = "subscription"  # frozen dataclass


# --- manage.status ---


def test_status_reports_the_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    assert manage.status(tmp_path)["preference"] == "auto"  # virgin home
    manage.set_preference(tmp_path, "api_key")
    assert manage.status(tmp_path)["preference"] == "api_key"


# --- CLI: lohra auth prefer ---


def test_auth_prefer_writes_the_value(tmp_path, monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)  # else home re-roots under it
    assert cli.run_auth("prefer", value="subscription") == 0
    assert store.read_config(tmp_path).preference == "subscription"
    assert "subscription" in capsys.readouterr().out


@pytest.mark.parametrize("value", [None, "", "bogus", "API_KEY"])
def test_auth_prefer_refuses_an_invalid_value_didactically(tmp_path, monkeypatch, capsys, value):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)  # else home re-roots under it
    assert cli.run_auth("prefer", value=value) == 2
    err = capsys.readouterr().err
    assert "auto" in err and "subscription" in err and "api_key" in err
    assert store.read_config(tmp_path) is None  # nothing written


def test_auth_prefer_keeps_the_tos_acknowledgement_unlike_disable(tmp_path, monkeypatch):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)  # else home re-roots under it
    manage.enable(tmp_path)
    assert cli.run_auth("prefer", value="api_key") == 0
    cfg = store.read_config(tmp_path)
    # going back to the subscription is one command, not a re-acceptance of the ToS
    assert cfg.acknowledged_tos_risk is True and cfg.auth_mode == "subscription"
    assert cli.run_auth("prefer", value="auto") == 0
    assert subscription_active(tmp_path) is True


def test_auth_status_prints_the_preference(tmp_path, monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)  # else home re-roots under it
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    manage.set_preference(tmp_path, "api_key")
    assert cli.run_auth("status") == 0
    assert json.loads(capsys.readouterr().out)["preference"] == "api_key"


def test_auth_prefer_is_a_parseable_subcommand():
    from lohra import cli

    args = cli.build_parser().parse_args(["auth", "prefer", "api_key"])
    assert args.command == "auth" and args.action == "prefer" and args.value == "api_key"


# --- CLI wiring: run_chat / build_dashboard_app take the preferred route ---


@pytest.fixture
def keyless(monkeypatch, tmp_path):
    """A home with an ACTIVE subscription and no API key anywhere."""
    monkeypatch.setattr("lohra.memory.paths.lohra_home", lambda: tmp_path)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOHRA_PROVIDER", "LOHRA_MODEL"):
        monkeypatch.delenv(var, raising=False)

    def _boom(home, **kw):
        raise AssertionError("SENTINEL: the subscription client must NOT be built")

    monkeypatch.setattr("lohra.subscription.provider.build_subscription_client", _boom)
    return tmp_path


def test_run_chat_honors_preference_api_key_over_an_active_subscription(keyless, capsys):
    from lohra import cli

    _raw_auth_json(keyless, _active_entry(preference="api_key"))
    code = cli.run_chat("hello", no_input=True)
    err = capsys.readouterr().err
    assert code == 2  # died on the API-key path (no key configured), as intended
    assert "SENTINEL" not in err  # never touched the subscription client
    assert err.count("preference=api_key") == 1  # ONE note, not one per read


def test_run_chat_fails_loudly_when_the_preferred_subscription_is_unusable(keyless, capsys):
    from lohra import cli

    _raw_auth_json(keyless, _inactive_entry(preference="subscription"))
    code = cli.run_chat("hello", no_input=True)
    err = capsys.readouterr().err
    assert code == 2
    assert "lohra auth enable" in err and "lohra auth login" in err
    assert "SENTINEL" not in err


def test_run_chat_json_wraps_the_preference_error_in_one_envelope(keyless, capsys):
    from lohra import cli

    _raw_auth_json(keyless, _inactive_entry(preference="subscription"))
    code = cli.run_chat("hello", json_output=True, no_input=True)
    out = capsys.readouterr().out
    assert code == 2
    envelope = json.loads(out)  # stdout is ALWAYS exactly one parseable object
    assert envelope["completed"] is False and "lohra auth enable" in envelope["error"]


def test_dashboard_honors_preference_api_key_over_an_active_subscription(keyless, capsys):
    from lohra import cli

    _raw_auth_json(keyless, _active_entry(preference="api_key"))
    _, app, code = cli.build_dashboard_app(insecure=True)
    err = capsys.readouterr().err
    assert app is None and code == 2
    assert "SENTINEL" not in err and err.count("preference=api_key") == 1


def test_dashboard_fails_loudly_when_the_preferred_subscription_is_unusable(keyless, capsys):
    from lohra import cli

    _raw_auth_json(keyless, _inactive_entry(preference="subscription"))
    _, app, code = cli.build_dashboard_app(insecure=True)
    err = capsys.readouterr().err
    assert app is None and code == 2
    assert "lohra auth enable" in err and "SENTINEL" not in err
