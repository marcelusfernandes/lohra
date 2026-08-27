"""ONB-7 (keyless Ollama fallback) and ONB-9 (transparency + the cost footgun).

Two properties are load-bearing and every test here defends one of them:

* **A configured machine is byte-identical.** No probe, no extra line. The probe
  stub counts its calls so a regression into "always probe" fails loudly.
* **Nothing reaches stdout.** Both features write to stderr only, because stdout
  carries the agent's answer or the ``--json`` envelope.

No network, no sleep, no real ``$HOME``: the daemon probe is injected and the
subscription stores are real files under ``tmp_path``.
"""

import io
import json
import sys

import pytest

from lohra import cli
from lohra.onboarding import choice, detect


# --- fakes -------------------------------------------------------------------


class _Probe:
    """A stub Ollama probe that remembers how often it was asked."""

    def __init__(self, *models, alive=None):
        self.models = tuple(models)
        self.alive = bool(models) if alive is None else alive
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return detect.OllamaStatus(
            alive=self.alive, url=detect.OLLAMA_TAGS_URL, models=self.models
        )


def _dead():
    return _Probe(alive=False)


def _virgin_env(**extra):
    """An environment with no key, no provider var, no model — plus ``extra``."""
    return dict(extra)


def _write_subscription(home, *, active=True):
    """A real ``auth.json`` opt-in record — the same shape ``manage.enable`` writes."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"openai": {"auth_mode": "subscription", "acknowledged_tos_risk": active}}),
        encoding="utf-8",
    )


# --- ONB-7: the daemon IS the configuration ----------------------------------


def test_a_live_daemon_resolves_to_ollama_when_nothing_else_is_configured():
    probe = _Probe("llama3.2", "phi4")

    resolution = choice.resolve_choice(env=_virgin_env(), probe=probe)

    assert resolution.provider == "ollama"
    assert resolution.origin == choice.KEYLESS
    assert resolution.model == "llama3.2"  # the first pulled tag; ollama has no fallback
    assert resolution.error is None
    assert probe.calls == 1


def test_an_alive_daemon_with_nothing_pulled_is_not_a_provider():
    """`ollama` declares no fallback_models: choosing it with an empty daemon
    would trade today's actionable error for `has no default model — pass --model`."""
    resolution = choice.resolve_choice(env=_virgin_env(), probe=_Probe(alive=True))

    assert resolution.provider is None
    assert resolution.model is None
    assert resolution.error.startswith("no provider configured")


def test_a_dead_daemon_keeps_the_onb1_error_exactly():
    from lohra.onboarding.messages import NO_PROVIDER_CONFIGURED

    resolution = choice.resolve_choice(env=_virgin_env(), probe=_dead())

    assert resolution.provider is None
    assert resolution.origin == choice.NONE
    assert resolution.error == NO_PROVIDER_CONFIGURED


def test_a_configured_machine_is_never_probed_at_all():
    """ONB-7's other half: with config, this feature must add NOTHING."""
    probe = _Probe("llama3.2")

    resolution = choice.resolve_choice(env=_virgin_env(ANTHROPIC_API_KEY="sk-x"), probe=probe)

    assert probe.calls == 0
    assert resolution.provider == "anthropic"
    assert resolution.origin == choice.API_KEY
    assert resolution.detail == "ANTHROPIC_API_KEY"
    assert resolution.model is None  # the provider's own fallback still decides


def test_an_explicit_argument_beats_a_live_daemon_and_skips_the_probe():
    probe = _Probe("llama3.2")

    resolution = choice.resolve_choice("openai", env=_virgin_env(), probe=probe)

    assert (resolution.provider, resolution.origin) == ("openai", choice.FLAG)
    assert probe.calls == 0


def test_the_provider_env_var_is_its_own_origin_and_skips_the_probe():
    probe = _Probe("llama3.2")

    resolution = choice.resolve_choice(env=_virgin_env(LOHRA_PROVIDER="groq"), probe=probe)

    assert (resolution.provider, resolution.origin) == ("groq", choice.ENV_VAR)
    assert probe.calls == 0


def test_an_unknown_provider_still_raises_for_the_caller_to_report():
    """A user who named a provider made a choice; it deserves the typo error."""
    with pytest.raises(ValueError):
        choice.resolve_choice("totally-bogus", env=_virgin_env(), probe=_dead())


def test_an_alias_resolves_to_the_canonical_name():
    resolution = choice.resolve_choice("google", env=_virgin_env(), probe=_dead())
    assert resolution.provider == "gemini"


# --- ONB-9 (a): say what was chosen, and why ---------------------------------


def test_the_transparency_line_names_provider_model_and_the_reason():
    resolution = choice.resolve_choice(env=_virgin_env(OPENAI_API_KEY="sk-x"), probe=_dead())

    line = choice.transparency_line(resolution, "gpt-4o-mini")

    assert "openai" in line and "gpt-4o-mini" in line
    assert "OPENAI_API_KEY" in line  # the *why*, not just the *what*
    assert "\n" not in line  # exactly one line


def test_no_transparency_line_when_the_user_named_the_provider():
    """Silence is the contract for an explicit choice: you already know."""
    for arg, env in (("openai", {}), (None, {"LOHRA_PROVIDER": "groq"})):
        resolution = choice.resolve_choice(arg, env=_virgin_env(**env), probe=_dead())
        assert choice.transparency_line(resolution, "m") is None


def test_the_keyless_line_says_it_found_no_key_and_how_to_pin_it():
    resolution = choice.resolve_choice(env=_virgin_env(), probe=_Probe("llama3.2"))

    line = choice.transparency_line(resolution, "llama3.2")

    assert "ollama" in line and "llama3.2" in line
    assert "--provider ollama" in line or "LOHRA_PROVIDER=ollama" in line  # how to pin
    assert "no API key" in line


def test_a_resolution_without_a_provider_has_no_transparency_line():
    resolution = choice.resolve_choice(env=_virgin_env(), probe=_dead())
    assert choice.transparency_line(resolution, None) is None


# --- ONB-9 (b): the cost footgun --------------------------------------------


def test_the_cost_warning_fires_only_on_a_real_divergence(tmp_path):
    base, home = tmp_path / "lohra", tmp_path / "lohra" / "profiles" / "work"
    _write_subscription(base)
    home.mkdir(parents=True)

    warning = choice.cost_warning(base=base, home=home, profile="work")

    assert warning is not None
    assert "lohra auth enable --profile work" in warning
    assert "work" in warning
    assert "\n" not in warning.strip()  # one line, on stderr


def test_no_cost_warning_without_an_active_profile(tmp_path):
    base = tmp_path / "lohra"
    _write_subscription(base)
    assert choice.cost_warning(base=base, home=base, profile=None) is None


def test_no_cost_warning_when_the_profile_has_its_own_subscription(tmp_path):
    base, home = tmp_path / "lohra", tmp_path / "lohra" / "profiles" / "work"
    _write_subscription(base)
    _write_subscription(home)
    assert choice.cost_warning(base=base, home=home, profile="work") is None


def test_no_cost_warning_when_the_base_never_had_a_subscription(tmp_path):
    """No divergence, no warning: a paid key everywhere is not a surprise."""
    base, home = tmp_path / "lohra", tmp_path / "lohra" / "profiles" / "work"
    home.mkdir(parents=True)
    assert choice.cost_warning(base=base, home=home, profile="work") is None


def test_an_unacknowledged_base_opt_in_does_not_count_as_a_subscription(tmp_path):
    """Fail-closed mirror: the base store is only "on" when the ToS was accepted."""
    base, home = tmp_path / "lohra", tmp_path / "lohra" / "profiles" / "work"
    _write_subscription(base, active=False)
    home.mkdir(parents=True)
    assert choice.cost_warning(base=base, home=home, profile="work") is None


# --- wiring: `lohra chat` ----------------------------------------------------


@pytest.fixture()
def virgin(monkeypatch, tmp_path):
    """A machine with no key, no subscription, no profile — and its own home."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "lohra"))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
                "DEEPSEEK_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "GEMINI_API_KEY",
                "GOOGLE_API_KEY", "OLLAMA_API_KEY", "LOHRA_PROVIDER", "LOHRA_MODEL",
                "LOHRA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOHRA_NO_WIZARD", "1")  # the wizard has its own test file
    return tmp_path / "lohra"


def _pin_probe(monkeypatch, probe):
    monkeypatch.setattr(detect, "default_probe", probe)


def _fake_client(monkeypatch, text="olá"):
    """A client whose reply parses under BOTH transports.

    The keyless path lands on ollama (``chat_completions``) while the API-key path
    here lands on anthropic (``anthropic_messages``); carrying both shapes keeps
    one helper honest for every test in this file.
    """
    from lohra import agent as agent_pkg

    class _Fake(agent_pkg.ModelClient):
        def create(self, **kwargs):
            return {
                "content": [{"type": "text", "text": text}],  # anthropic_messages
                "stop_reason": "end_turn",
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": None,
            }

    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: _Fake())


def _run(monkeypatch, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    code = cli.run_chat(kwargs.pop("prompt", "oi"), use_tools=False, **kwargs)
    return code, out.getvalue(), err.getvalue()


def test_chat_answers_on_a_keyless_machine_with_ollama_running(monkeypatch, virgin):
    """THE ONB-7 aceite: no key, no subscription, daemon up -> a real answer."""
    _pin_probe(monkeypatch, _Probe("llama3.2"))
    _fake_client(monkeypatch)

    code, out, err = _run(monkeypatch)

    assert code == 0
    assert "olá" in out
    assert "ollama" in err and "llama3.2" in err  # announced, on stderr
    assert "ollama" not in out  # never on stdout


def test_the_keyless_choice_stays_out_of_the_json_envelope(monkeypatch, virgin):
    """stdout under --json is exactly one object; the notice lives on stderr."""
    _pin_probe(monkeypatch, _Probe("llama3.2"))
    _fake_client(monkeypatch)

    code, out, err = _run(monkeypatch, json_output=True)

    assert code == 0
    envelope = json.loads(out)  # one object, nothing else
    assert envelope["completed"] is True
    assert "ollama" in err


def test_a_dead_daemon_leaves_the_error_path_untouched(monkeypatch, virgin):
    _pin_probe(monkeypatch, _dead())

    code, _out, err = _run(monkeypatch)

    assert code == 2
    assert "no provider configured" in err


def test_a_configured_chat_never_probes_and_says_which_key_won(monkeypatch, virgin):
    probe = _Probe("llama3.2")
    _pin_probe(monkeypatch, probe)
    _fake_client(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")

    code, _out, err = _run(monkeypatch)

    assert code == 0
    assert probe.calls == 0  # byte-identical: config means no probe
    assert "anthropic" in err and "ANTHROPIC_API_KEY" in err


def test_an_explicit_provider_gets_no_transparency_line(monkeypatch, virgin):
    _pin_probe(monkeypatch, _dead())
    _fake_client(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")

    code, _out, err = _run(monkeypatch, provider="anthropic")

    assert code == 0
    assert "auto-detected" not in err


def test_chat_warns_once_when_this_profile_will_bill_a_paid_key(monkeypatch, tmp_path):
    """ONB-9 aceite: the warning appears exactly once, and only on divergence."""
    base = tmp_path / "lohra"
    monkeypatch.setenv("LOHRA_HOME", str(base))
    monkeypatch.setenv("LOHRA_PROFILE", "work")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    _write_subscription(base)  # the shared home rides a subscription; the profile does not
    _pin_probe(monkeypatch, _dead())
    _fake_client(monkeypatch)

    code, out, err = _run(monkeypatch)

    assert code == 0
    assert err.count("lohra auth enable --profile work") == 1
    assert "lohra auth enable --profile work" not in out


def test_no_cost_warning_when_the_profile_shares_the_subscription(monkeypatch, tmp_path):
    base = tmp_path / "lohra"
    monkeypatch.setenv("LOHRA_HOME", str(base))
    monkeypatch.setenv("LOHRA_PROFILE", "work")
    _write_subscription(base)
    _write_subscription(base / "profiles" / "work")
    _pin_probe(monkeypatch, _dead())

    # Subscription mode is active for this profile: it never reaches the API-key
    # path at all, so it must not be told it is about to pay for one. (The token
    # itself is absent, so the run fails on the credential — that is not the point.)
    _code, _out, err = _run(monkeypatch)

    assert "auth enable" not in err


def test_profile_create_points_at_the_subscription_it_will_not_inherit(monkeypatch, tmp_path, capsys):
    """ONB-9 (c): the suggestion lands where the profile is born."""
    base = tmp_path / "lohra"
    monkeypatch.setenv("LOHRA_HOME", str(base))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    _write_subscription(base)

    assert cli.run_profile("create", name="work") == 0
    assert "lohra auth enable --profile work" in capsys.readouterr().out


def test_profile_create_stays_quiet_without_a_base_subscription(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "lohra"))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)

    assert cli.run_profile("create", name="work") == 0
    assert "auth enable" not in capsys.readouterr().out
