"""Onboarding fatia B — `lohra init`, the first-run wizard, and the headless contract.

Determinism rules for this whole file, deliberately strict:

* **No real network.** The Ollama probe is always an injected stub.
* **No real terminal.** Prompts read from a fake stdin stream and write to a fake
  stderr; ``builtins.input`` is never monkeypatched (the wizard takes streams).
* **No real ``$HOME``.** ``LOHRA_HOME``/``base``/``user_home`` are pinned to tmp_path.
* **No sleeping.**

The single most important test in the file is
``test_json_mode_never_prompts_and_stdout_stays_one_object``: `lohra chat --json`
is the orchestration surface, and a prompt leaking into it corrupts every
consumer.
"""

from __future__ import annotations

import dataclasses
import io
import json
from pathlib import Path

import pytest

from lohra.config.env_file import parse_env_text
from lohra.onboarding import detect, env_write


# --- helpers -----------------------------------------------------------------


class _FakeTTY(io.StringIO):
    """A stream that answers isatty() the way the test wants."""

    def __init__(self, text: str = "", *, tty: bool = True) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _Answers(_FakeTTY):
    """Scripted stdin: one line per prompt; exhausted -> EOF (= take the default)."""

    def __init__(self, *lines: str, tty: bool = True) -> None:
        super().__init__("".join(line + "\n" for line in lines), tty=tty)


def _dead_ollama() -> detect.OllamaStatus:
    return detect.OllamaStatus(alive=False, url=detect.OLLAMA_TAGS_URL, detail="stub")


def _live_ollama(*models: str):
    def probe() -> detect.OllamaStatus:
        return detect.OllamaStatus(alive=True, url=detect.OLLAMA_TAGS_URL, models=models)

    return probe


def _snapshot(tmp_path: Path, **kwargs):
    params = dict(
        env={},
        base=tmp_path / "lohra",
        user_home=tmp_path / "user",
        ollama_probe=_dead_ollama,
        which=lambda name: None,
        stdin=_FakeTTY(tty=True),
        stderr=_FakeTTY(tty=True),
    )
    params.update(kwargs)
    return detect.detect_environment(**params)


# === env_write — the missing half of config/env_file (it only had a reader) ===


def test_writes_a_new_env_file_that_the_reader_can_parse_back(tmp_path):
    path = tmp_path / ".env"
    written = env_write.upsert_env_file(path, {"OPENAI_API_KEY": "sk-abc"})

    assert written == ("OPENAI_API_KEY",)
    assert parse_env_text(path.read_text(encoding="utf-8")) == {"OPENAI_API_KEY": "sk-abc"}


def test_upsert_preserves_unrelated_lines_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# my keys\nANTHROPIC_API_KEY=sk-ant\n\nOPENAI_API_KEY=old\n", encoding="utf-8")

    env_write.upsert_env_file(path, {"OPENAI_API_KEY": "new"})

    text = path.read_text(encoding="utf-8")
    assert "# my keys" in text
    parsed = parse_env_text(text)
    assert parsed == {"ANTHROPIC_API_KEY": "sk-ant", "OPENAI_API_KEY": "new"}


def test_an_unchanged_value_is_not_rewritten(tmp_path):
    """Idempotence at the lowest level: same value in -> zero writes, same bytes."""
    path = tmp_path / ".env"
    env_write.upsert_env_file(path, {"OPENAI_API_KEY": "sk-abc"})
    before = path.read_bytes()

    assert env_write.upsert_env_file(path, {"OPENAI_API_KEY": "sk-abc"}) == ()
    assert path.read_bytes() == before


def test_a_duplicated_key_collapses_to_one_line(tmp_path):
    """parse_env_text is last-wins: a surviving later duplicate would beat our write."""
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=first\nOPENAI_API_KEY=second\n", encoding="utf-8")

    env_write.upsert_env_file(path, {"OPENAI_API_KEY": "third"})

    text = path.read_text(encoding="utf-8")
    assert text.count("OPENAI_API_KEY") == 1
    assert parse_env_text(text) == {"OPENAI_API_KEY": "third"}


def test_values_needing_quotes_round_trip(tmp_path):
    path = tmp_path / ".env"
    env_write.upsert_env_file(path, {"A": "two words", "B": "has#hash", "C": ""})

    assert parse_env_text(path.read_text(encoding="utf-8")) == {
        "A": "two words",
        "B": "has#hash",
        "C": "",
    }


def test_the_file_is_created_owner_only(tmp_path):
    import os
    import stat

    if os.name == "nt":  # chmod is a no-op on Windows; documented, not a failure
        pytest.skip("POSIX permissions only")
    path = tmp_path / "nested" / ".env"
    env_write.upsert_env_file(path, {"OPENAI_API_KEY": "sk-abc"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_temp_file_is_left_behind(tmp_path):
    path = tmp_path / ".env"
    env_write.upsert_env_file(path, {"K": "v"})
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


# === the prompter — streams in, streams out, `builtins.input` never touched ===


def test_enter_takes_the_default_and_the_question_goes_to_the_prompt_stream():
    from lohra.onboarding import wizard

    err = _FakeTTY()
    prompter = wizard.Prompter(_Answers(""), err)

    assert prompter.ask("provider", default="ollama") == "ollama"
    assert "provider" in err.getvalue() and "ollama" in err.getvalue()


def test_a_typed_answer_beats_the_default():
    from lohra.onboarding import wizard

    prompter = wizard.Prompter(_Answers("openai"), _FakeTTY())
    assert prompter.ask("provider", default="ollama") == "openai"


def test_end_of_input_falls_back_to_the_default_instead_of_hanging():
    """A closed/exhausted stdin must resolve, never block — the headless invariant."""
    from lohra.onboarding import wizard

    prompter = wizard.Prompter(_FakeTTY(""), _FakeTTY())
    assert prompter.ask("provider", default="ollama") == "ollama"
    assert prompter.confirm("configure now?", default=False) is False


def test_confirm_reads_yes_and_no_and_honors_its_default():
    from lohra.onboarding import wizard

    err = _FakeTTY()
    assert wizard.Prompter(_Answers("y"), err).confirm("go?", default=False) is True
    assert wizard.Prompter(_Answers("n"), err).confirm("go?", default=True) is False
    assert wizard.Prompter(_Answers(""), err).confirm("go?", default=True) is True


# === the marker — per-profile, and never written by a read-only run ===


def test_the_marker_lives_in_the_active_home_not_the_base(tmp_path, monkeypatch):
    """A fresh `--profile work` must still be offered the wizard: the marker is
    per-workspace, exactly like every other signal the wizard reads."""
    from lohra.memory.paths import lohra_home
    from lohra.onboarding import wizard

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_PROFILE", "work")

    assert wizard.marker_path(lohra_home()) == tmp_path / "profiles" / "work" / ".initialized"


def test_writing_the_marker_creates_a_missing_home(tmp_path):
    from lohra.onboarding import wizard

    home = tmp_path / "never" / "existed"
    assert not wizard.marker_present(home)
    wizard.write_marker(home)
    assert wizard.marker_present(home)


# === `lohra init` (ONB-3) ===


def _init(tmp_path, *answers, no_input=False, snapshot=None, environ=None, **kwargs):
    """run_init with every stream and path pinned; returns (code, out, err, environ)."""
    from lohra.onboarding import wizard

    env = {} if environ is None else environ
    snap = snapshot if snapshot is not None else _snapshot(tmp_path, env=env, **kwargs)
    out, err = _FakeTTY(), _FakeTTY()
    code = wizard.run_init(
        no_input=no_input,
        out=out,
        err=err,
        reader=_Answers(*answers),
        snapshot=snap,
        base=tmp_path / "lohra",
        home=tmp_path / "lohra",
        environ=env,
    )
    return code, out.getvalue(), err.getvalue(), env


def test_init_reports_what_it_found(tmp_path):
    code, out, _err, _env = _init(tmp_path, "", "")
    assert code == 0
    for label in ("python", "provider", "ollama", "subscription", "harnesses"):
        assert label in out.lower()
    assert str(tmp_path) in out  # the real paths, not a description of them


def test_virgin_machine_with_ollama_running_ends_usable_on_enter_alone(tmp_path):
    """THE ONB-3 aceite: Enter on every question leaves a working configuration."""
    code, out, _err, env = _init(tmp_path, "", "", "", ollama_probe=_live_ollama("llama3.2"))

    assert code == 0
    assert env["LOHRA_PROVIDER"] == "ollama"
    assert env["LOHRA_MODEL"] == "llama3.2"  # the pulled model, detected not asked
    written = parse_env_text((tmp_path / "lohra" / ".env").read_text(encoding="utf-8"))
    assert written["LOHRA_PROVIDER"] == "ollama" and written["LOHRA_MODEL"] == "llama3.2"
    assert "ready" in out.lower()


def test_virgin_machine_with_nothing_prints_exactly_what_is_missing(tmp_path):
    """The other half of the aceite: no usable default -> say what is missing."""
    from lohra.onboarding.messages import NO_PROVIDER_CONFIGURED

    code, out, _err, env = _init(tmp_path, "", "", "")

    assert code == 0
    assert env == {}  # nothing invented, nothing written
    assert not (tmp_path / "lohra" / ".env").exists()
    assert NO_PROVIDER_CONFIGURED.splitlines()[0] in out


def test_init_collects_a_key_for_a_provider_that_needs_one(tmp_path):
    code, _out, _err, env = _init(tmp_path, "openai", "sk-typed", "")

    assert code == 0
    assert env["OPENAI_API_KEY"] == "sk-typed"
    assert env["LOHRA_PROVIDER"] == "openai"
    body = (tmp_path / "lohra" / ".env").read_text(encoding="utf-8")
    assert parse_env_text(body)["OPENAI_API_KEY"] == "sk-typed"


def test_init_asks_at_most_three_questions(tmp_path):
    """Scope cap. Every prompt line ends with the same marker, so they are countable."""
    from lohra.onboarding import wizard

    _code, _out, err, _env = _init(
        tmp_path,
        "openai",
        "sk-typed",
        "",
        which=lambda name: f"/usr/bin/{name}",
        ollama_probe=_live_ollama("llama3.2"),
    )
    assert err.count(wizard.PROMPT_SUFFIX) <= 3


def test_a_key_that_is_already_set_turns_question_two_into_the_model(tmp_path):
    code, _out, _err, env = _init(tmp_path, "", "gpt-4o-mini", environ={"OPENAI_API_KEY": "sk-x"})

    assert code == 0
    assert env["LOHRA_MODEL"] == "gpt-4o-mini"
    assert "LOHRA_PROVIDER" not in env  # openai was already the detected provider


def test_enter_on_the_model_does_not_freeze_the_provider_default_into_the_file(tmp_path):
    """Enter means 'keep as is'. Pinning fallback_models[0] would freeze a default
    that is supposed to float with the provider profile."""
    code, _out, _err, env = _init(tmp_path, "", "", environ={"OPENAI_API_KEY": "sk-x"})

    assert code == 0
    assert "LOHRA_MODEL" not in env
    assert not (tmp_path / "lohra" / ".env").exists()


def test_an_unknown_provider_answer_keeps_the_default_instead_of_writing_junk(tmp_path):
    """A typo must not become a persisted LOHRA_PROVIDER nobody can resolve."""
    code, _out, err, env = _init(tmp_path, "not-a-provider", "", "")
    assert code == 0
    assert "LOHRA_PROVIDER" not in env
    assert "not-a-provider" in err


def test_init_is_idempotent_a_second_all_enter_run_writes_nothing(tmp_path):
    """ONB-3: rodar de novo é no-op — same bytes, no duplicated key."""
    env: dict[str, str] = {}
    _init(tmp_path, "openai", "sk-typed", "", environ=env)
    envfile = tmp_path / "lohra" / ".env"
    before = envfile.read_bytes()

    code, _out, _err, env2 = _init(tmp_path, "", "", "", environ=dict(env))

    assert code == 0
    assert envfile.read_bytes() == before


def test_init_writes_the_marker_so_the_chat_wizard_never_double_asks(tmp_path):
    from lohra.onboarding import wizard

    _init(tmp_path, "", "", "")
    assert wizard.marker_present(tmp_path / "lohra")


def test_init_without_a_tty_is_a_read_only_report(tmp_path):
    """No prompt, no write, no marker — and still exit 0."""
    from lohra.onboarding import wizard

    snap = _snapshot(tmp_path, stdin=_FakeTTY(tty=False), stderr=_FakeTTY(tty=False))
    code, out, err, env = _init(tmp_path, "openai", "sk-typed", snapshot=snap)

    assert code == 0
    assert env == {} and err == ""
    assert not (tmp_path / "lohra" / ".env").exists()
    assert not wizard.marker_present(tmp_path / "lohra")
    assert "python" in out.lower()


def test_init_with_no_input_is_a_read_only_report_even_on_a_tty(tmp_path):
    from lohra.onboarding import wizard

    code, _out, err, env = _init(tmp_path, "openai", "sk-typed", no_input=True)

    assert code == 0
    assert env == {} and err == ""
    assert not wizard.marker_present(tmp_path / "lohra")


def test_the_kit_is_offered_only_when_a_harness_is_there_and_only_on_a_yes(tmp_path):
    home = tmp_path / "user"
    (home / ".claude").mkdir(parents=True)
    # Two prompts only: provider (Enter -> nothing detected) then the kit.
    code, _out, err, _env = _init(
        tmp_path, "", "y", which=lambda name: "/usr/bin/claude" if name == "claude" else None
    )
    assert code == 0
    assert (home / ".claude" / "skills" / "use-lohra" / "SKILL.md").is_file()
    assert "use-lohra" in err


def test_the_kit_is_not_written_on_enter(tmp_path):
    home = tmp_path / "user"
    (home / ".claude").mkdir(parents=True)
    _init(tmp_path, "", "", "", which=lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert not (home / ".claude" / "skills").exists()


def test_no_harness_no_question(tmp_path):
    from lohra.onboarding import wizard

    _code, _out, err, _env = _init(tmp_path, "", "")
    assert "use-lohra" not in err
    assert err.count(wizard.PROMPT_SUFFIX) <= 2


# === CLI wiring: the ONB-4 hook and the ONB-5 headless contract ===============

import sys  # noqa: E402 — the CLI tests below drive sys.std* deliberately

from lohra import cli  # noqa: E402


@pytest.fixture
def virgin(monkeypatch, tmp_path):
    """A machine with nothing configured, fully pinned: no $HOME, no key, no net."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LOHRA_PROVIDER", "LOHRA_MODEL",
                "LOHRA_NO_WIZARD", "LOHRA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _pin_detection(monkeypatch, snapshot):
    """No probe ever reaches the real machine from a CLI test."""
    monkeypatch.setattr(detect, "detect_environment", lambda **kw: snapshot)


def _forbid_wizard(monkeypatch):
    from lohra.onboarding import wizard

    def _boom(**kwargs):
        raise AssertionError("the wizard must not run here")

    monkeypatch.setattr(wizard, "offer_wizard", _boom)


def _fake_client(monkeypatch, text="olá do fake"):
    from lohra import agent as agent_pkg

    class _Fake(agent_pkg.ModelClient):
        def create(self, **kwargs):
            return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "usage": None}

    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: _Fake())


def _drive(monkeypatch, *answers, tty=True, **chat_kwargs):
    """run_chat with stdout/stderr/stdin replaced by fakes that own their tty-ness."""
    out, err = _FakeTTY(tty=tty), _FakeTTY(tty=tty)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "stdin", _Answers(*answers, tty=tty))
    code = cli.run_chat(**chat_kwargs)
    return code, out.getvalue(), err.getvalue()


# --- THE contract test -------------------------------------------------------


def test_json_mode_never_prompts_and_stdout_stays_one_object(monkeypatch, virgin):
    """The single most important test here.

    `lohra chat --json` is the orchestration surface: stdout is ALWAYS exactly one
    parseable object. A TTY is present and the machine is virgin — every condition
    for the wizard is met EXCEPT --json, which alone must close the gate.
    """
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)

    code, out, _err = _drive(monkeypatch, "y", "anthropic", "sk-typed", json_output=True,
                             prompt="oi")

    assert code == 2
    envelope = json.loads(out)  # one object, nothing else — json.loads proves both
    assert envelope["output"] is None and envelope["completed"] is False
    assert "lohra init" in envelope["error"]  # ONB-5: the headless error names the remedy
    assert not (virgin / ".initialized").exists()  # nothing was "asked", nothing remembered


# --- ONB-4: it fires, and the original prompt still runs ---------------------


def test_a_virgin_tty_gets_the_wizard_and_the_original_prompt_still_answers(monkeypatch, virgin):
    """ONB-4 aceite: `lohra chat "oi"` ends with the agent's answer, no intermediate
    command, nothing retyped."""
    _pin_detection(monkeypatch, _snapshot(virgin))
    _fake_client(monkeypatch)

    code, out, err = _drive(monkeypatch, "y", "anthropic", "sk-typed", prompt="oi",
                            use_tools=False)

    assert code == 0
    assert out.strip().endswith("olá do fake")  # the ORIGINAL prompt ran
    assert "configure now?" in err  # and the wizard lived entirely on stderr
    assert parse_env_text((virgin / ".env").read_text(encoding="utf-8"))["ANTHROPIC_API_KEY"] == "sk-typed"
    assert (virgin / ".initialized").is_file()


def test_declining_the_wizard_falls_through_to_the_onb1_error(monkeypatch, virgin):
    _pin_detection(monkeypatch, _snapshot(virgin))

    code, _out, err = _drive(monkeypatch, "n", prompt="oi")

    assert code == 2
    assert "no provider configured" in err
    assert (virgin / ".initialized").is_file()  # "no" is an answer; never re-ask


def test_the_marker_stops_a_second_offer(monkeypatch, virgin):
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)
    (virgin / ".initialized").write_text("x", encoding="utf-8")

    code, _out, err = _drive(monkeypatch, "y", prompt="oi")
    assert code == 2 and "no provider configured" in err


def test_a_configured_run_is_byte_identical_with_and_without_the_wizard(monkeypatch, virgin):
    """ONB-4 aceite: zero behaviour change when a provider already resolves.

    Same call twice — once with the wizard fully enabled on a TTY, once with it
    disabled by env — and both streams must match byte for byte.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    _fake_client(monkeypatch)
    _pin_detection(monkeypatch, _snapshot(virgin, env={"ANTHROPIC_API_KEY": "sk-real"}))

    # Same session id both times: the only remaining difference would be the wizard.
    with_wizard = _drive(monkeypatch, prompt="oi", use_tools=False, session="s1")
    monkeypatch.setenv("LOHRA_NO_WIZARD", "1")
    without = _drive(monkeypatch, prompt="oi", use_tools=False, session="s1")

    assert with_wizard == without
    assert not (virgin / ".initialized").exists()  # nothing to fix -> nothing offered


def test_an_unknown_provider_gets_its_own_error_not_a_wizard(monkeypatch, virgin):
    """A user who named a provider made a choice; don't answer it with a survey."""
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)

    code, _out, err = _drive(monkeypatch, "y", prompt="oi", provider="totally-bogus")
    assert code == 2 and "unknown provider" in err


# --- ONB-5: every way of saying "headless" ----------------------------------


def test_a_piped_stdin_never_asks_anything(monkeypatch, virgin):
    """`echo oi | lohra chat` — no terminal, no prompt, ever."""
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)

    code, _out, err = _drive(monkeypatch, "y", tty=False, prompt="oi")
    assert code == 2 and "no provider configured" in err


def test_no_input_closes_the_gate_on_a_real_tty(monkeypatch, virgin):
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)

    code, _out, _err = _drive(monkeypatch, "y", prompt="oi", no_input=True)
    assert code == 2


def test_lohra_no_wizard_closes_the_gate_on_a_real_tty(monkeypatch, virgin):
    monkeypatch.setenv("LOHRA_NO_WIZARD", "1")
    _pin_detection(monkeypatch, _snapshot(virgin))
    _forbid_wizard(monkeypatch)

    code, _out, err = _drive(monkeypatch, "y", prompt="oi")
    assert code == 2 and "lohra init" in err  # the headless error names the remedy


def test_no_input_is_a_global_flag_on_every_command_that_can_prompt():
    parser = cli.build_parser()
    for argv in (["chat", "oi"], ["init"], ["auth", "status"], ["dashboard"], ["serve"]):
        assert parser.parse_args(argv).no_input is False
    assert parser.parse_args(["chat", "oi", "--no-input"]).no_input is True


# --- `lohra init` as a subcommand -------------------------------------------


def test_init_is_dispatched_and_stays_read_only_under_no_input(monkeypatch, virgin, capsys):
    _pin_detection(monkeypatch, _snapshot(virgin))

    assert cli.main(["init", "--no-input"]) == 0
    assert "python" in capsys.readouterr().out.lower()
    assert not (virgin / ".initialized").exists()


# --- the model knob the wizard writes has to actually be read ---------------


def test_lohra_model_is_honored_by_chat(monkeypatch, virgin):
    """The wizard persists LOHRA_MODEL; `lohra chat` must consume it, or the
    question writes a value that does nothing (the dashboard already reads it)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("LOHRA_MODEL", "claude-from-the-wizard")
    _fake_client(monkeypatch)

    code, out, _err = _drive(monkeypatch, prompt="oi", provider="anthropic",
                             use_tools=False, json_output=True)
    assert code == 0
    assert json.loads(out)["model"] == "claude-from-the-wizard"


def test_an_explicit_model_flag_still_beats_the_env(monkeypatch, virgin):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("LOHRA_MODEL", "from-env")
    _fake_client(monkeypatch)

    _code, out, _err = _drive(monkeypatch, prompt="oi", provider="anthropic", model="from-flag",
                              use_tools=False, json_output=True)
    assert json.loads(out)["model"] == "from-flag"


# --- headless honesty for the one command that could hang for 10 minutes ----


def test_auth_login_under_no_input_fails_fast_instead_of_polling(monkeypatch, virgin):
    """`lohra auth login` polls for up to 600s. Headless, that is a hang, not a login."""
    from lohra.subscription import oauth

    def _boom(*a, **kw):
        raise AssertionError("the device flow must not start headless")

    monkeypatch.setattr(oauth, "start_device_login", _boom)
    err = _FakeTTY()
    monkeypatch.setattr(sys, "stderr", err)

    assert cli.run_auth("login", no_input=True) != 0
    assert "lohra auth login" in err.getvalue() or "terminal" in err.getvalue()


def test_auth_enable_under_no_input_aborts_instead_of_asking(monkeypatch, virgin):
    from lohra.subscription import manage

    monkeypatch.setattr(sys, "stderr", _FakeTTY())
    monkeypatch.setattr(sys, "stdout", _FakeTTY())

    assert cli.run_auth("enable", no_input=True) != 0
    assert manage.status(virgin).get("active") is not True


# === defensive edges: onboarding may degrade, never crash ====================


def test_a_value_containing_both_quote_styles_is_emitted_raw(tmp_path):
    """No escaping scheme exists in parse_env_text, so the writer must not invent
    one it cannot read back. API keys never look like this; the branch still must
    not corrupt the file."""
    path = tmp_path / ".env"
    env_write.upsert_env_file(path, {"K": "a\"b'c"})
    assert path.read_text(encoding="utf-8").startswith("K=")


def test_a_writer_that_explodes_does_not_kill_the_prompt():
    from lohra.onboarding import wizard

    class _Broken:
        def write(self, text):
            raise BrokenPipeError("gone")

        def flush(self):
            raise BrokenPipeError("gone")

    assert wizard.Prompter(_Answers("openai"), _Broken()).ask("provider", default="") == "openai"


def test_a_reader_that_explodes_reads_as_end_of_input():
    from lohra.onboarding import wizard

    class _Broken:
        def readline(self):
            raise OSError("detached")

    assert wizard.Prompter(_Broken(), _FakeTTY()).ask("provider", default="ollama") == "ollama"


def test_an_unwritable_harness_home_is_reported_not_raised(tmp_path, monkeypatch):
    def _explode(name, dest):
        raise OSError("read-only file system")

    monkeypatch.setattr("lohra.skills.exportkit.write_exportable", _explode)
    (tmp_path / "user" / ".claude").mkdir(parents=True)

    code, _out, err, _env = _init(
        tmp_path, "", "y", which=lambda name: "/usr/bin/claude" if name == "claude" else None
    )
    assert code == 0
    assert "read-only file system" in err


def test_a_provider_resolution_error_shows_up_in_the_report(tmp_path):
    """A bad LOHRA_PROVIDER must be explained by the report, not raised by it."""
    code, out, _err, _env = _init(tmp_path, "", "", environ={"LOHRA_PROVIDER": ""})
    assert code == 0 and "provider" in out.lower()


def test_a_marker_path_that_cannot_be_read_counts_as_absent(tmp_path):
    from lohra.onboarding import wizard

    # A file where the home should be: `<file>/.initialized` cannot be stat'ed.
    home = tmp_path / "not-a-dir"
    home.write_text("x", encoding="utf-8")
    assert wizard.marker_present(home) is False
    wizard.write_marker(home)  # must not raise either


# === the outcome line must describe the CHOSEN provider, not the machine ======
# Found on a real pty: configuring `ollama` with the daemon down printed the
# generic "no provider configured", which is false — a provider WAS configured.


def test_choosing_ollama_while_the_daemon_is_down_names_the_daemon(tmp_path):
    code, out, _err, env = _init(tmp_path, "ollama", "llama3.2", "")

    assert code == 0
    assert env["LOHRA_PROVIDER"] == "ollama"
    assert "no provider configured" not in out  # it IS configured
    assert "ollama serve" in out and detect.OLLAMA_TAGS_URL in out


def test_skipping_the_key_names_the_variable_that_is_still_missing(tmp_path):
    code, out, _err, _env = _init(tmp_path, "openai", "", "")

    assert code == 0
    assert "no provider configured" not in out
    assert "OPENAI_API_KEY" in out


def test_a_provider_choice_is_not_declared_ready_by_an_unrelated_key(tmp_path):
    """A key for a provider you did not choose does not make your choice work."""
    code, out, _err, _env = _init(tmp_path, "ollama", "llama3.2", "",
                                  environ={"ANTHROPIC_API_KEY": "sk-ant"})

    assert code == 0
    assert "ready" not in out.lower()
    assert "ollama serve" in out


def test_an_active_subscription_reports_ready_without_any_provider(tmp_path):
    """The no-API-key path is a configured state, not a missing one."""
    snap = dataclasses.replace(_snapshot(tmp_path), subscription_active=True)
    code, out, _err, _env = _init(tmp_path, "", snapshot=snap)

    assert code == 0
    assert "ready" in out.lower() and "subscription" in out.lower()
