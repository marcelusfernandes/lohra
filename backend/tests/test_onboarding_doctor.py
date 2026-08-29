"""ONB-6 — `lohra doctor`: a diagnosis you can act on, at any moment.

The dor being fixed is silence: `workflow_policy.json` missing turns every leaf
deny-by-default without naming the file; a malformed `mcp.json` logs a warning
that drowns in the chat stream; an absent `.env` is a silent no-op. So the whole
aceite is about *actionability*, and the tests enforce it structurally:

* every non-ok line carries a copyable **command**, not a description;
* no check can raise, whatever garbage the home holds;
* it never prompts, never opens a client, never spends a token;
* the exit code answers one question — can Lohra answer you right now?
* `--json` is exactly one object on stdout, with the same exit code.

Everything is injected: the environment, the home, the daemon probe, PATH and
even the Python version, so a run is identical on any machine.
"""

import io
import json

import pytest

from lohra import cli
from lohra.onboarding import detect, doctor


# --- fixtures ----------------------------------------------------------------


def _dead():
    return detect.OllamaStatus(alive=False, url=detect.OLLAMA_TAGS_URL, detail="ConnectError")


def _live(*models):
    return detect.OllamaStatus(alive=True, url=detect.OLLAMA_TAGS_URL, models=models)


class _Rude(io.StringIO):
    """A stream that punishes anyone who tries to read from or prompt on it."""

    def isatty(self):
        return True  # the most dangerous case: a terminal IS attached

    def readline(self, *args):
        raise AssertionError("doctor must never read from stdin")


def _snapshot(home, *, env=None, ollama=None, which=lambda name: None, version=(3, 12, 3)):
    """A snapshot pinned to ``home``; nothing here touches the real machine."""
    return detect.detect_environment(
        env=dict(env or {}),
        base=home,
        user_home=home / "user",
        ollama_probe=lambda: ollama or _dead(),
        which=which,
        stdin=io.StringIO(),
        stderr=io.StringIO(),
        version_info=version,
    )


def _by_name(checks):
    return {check.name: check for check in checks}


def _write_subscription(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"openai": {"auth_mode": "subscription", "acknowledged_tos_risk": True}}),
        encoding="utf-8",
    )


# --- the contract ------------------------------------------------------------


def test_every_non_ok_line_carries_a_copyable_command(tmp_path):
    """THE aceite. A warn/fail without a command is a bug, not a diagnosis."""
    checks = doctor.run_checks(_snapshot(tmp_path), env={})

    non_ok = [check for check in checks if check.state != doctor.OK]
    assert non_ok, "a virgin machine must have something to say"
    for check in non_ok:
        assert check.remedy, f"{check.name} is {check.state} with no remedy"
        assert any(
            word in check.remedy
            for word in ("lohra ", "export ", "ollama ", "python", "printf", "pip ")
        ), f"{check.name} remedy is not a command: {check.remedy!r}"


def test_every_lohra_remedy_actually_parses(tmp_path):
    """A remedy the user has to debug is not a remedy.

    Found live: the backlog's literal `lohra --profile <name> auth enable` does
    NOT parse — `--profile` is a subcommand option, not a global one. This test
    runs every emitted `lohra ...` command through the real parser, so the whole
    class of "copyable command that does not run" cannot come back.
    """
    import shlex

    _write_subscription(tmp_path)  # opt-in without a login -> `lohra auth login`
    (tmp_path / "profiles" / "work").mkdir(parents=True, exist_ok=True)
    scenarios = [
        _snapshot(tmp_path),                                            # virgin
        _snapshot(tmp_path, env={"LOHRA_PROFILE": "work"}),             # cost divergence
        _snapshot(tmp_path, which=lambda name: f"/usr/bin/{name}"),     # harness export
    ]

    seen = 0
    for snapshot in scenarios:
        for check in doctor.run_checks(snapshot, env={}):
            command = check.remedy.split("#")[0].strip()
            if not command.startswith("lohra "):
                continue
            seen += 1
            cli.build_parser().parse_args(shlex.split(command)[1:])  # SystemExit = broken
    assert seen >= 3


def test_the_remedies_reach_the_rendered_output(tmp_path):
    checks = doctor.run_checks(_snapshot(tmp_path), env={})
    text = doctor.render(checks)

    for check in checks:
        assert check.name in text
        if check.remedy:
            assert check.remedy in text


def test_no_check_raises_on_a_hostile_home(tmp_path):
    """Every config file is garbage, one is a directory. Diagnosis still runs."""
    home = tmp_path
    (home / "mcp.json").write_text("{ not json", encoding="utf-8")
    (home / "workflow_tiers.json").write_text("[]]", encoding="utf-8")
    (home / "auth.json").mkdir()
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text("\x00\x01", encoding="utf-8")

    checks = doctor.run_checks(_snapshot(home), env={})

    assert len(checks) >= 10
    assert doctor.render(checks)  # and it renders


def test_doctor_never_prompts_and_never_builds_a_client(monkeypatch, tmp_path):
    """"Roda sem gastar token": no client, no network, no question."""
    monkeypatch.setattr(
        "lohra.agent.client.build_client",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("doctor built a client")),
    )
    monkeypatch.setattr("sys.stdin", _Rude())

    out = io.StringIO()
    doctor.run_doctor(out=out, snapshot=_snapshot(tmp_path), env={})

    assert out.getvalue()


# --- the exit code: can Lohra answer you right now? --------------------------


def test_exit_2_when_nothing_at_all_can_answer(tmp_path):
    out = io.StringIO()
    code = doctor.run_doctor(out=out, snapshot=_snapshot(tmp_path), env={})

    assert code == 2
    assert _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))["provider"].state == doctor.FAIL
    assert "lohra init" in out.getvalue()


def test_exit_0_with_an_api_key(tmp_path):
    env = {"ANTHROPIC_API_KEY": "sk-x"}
    checks = doctor.run_checks(_snapshot(tmp_path, env=env), env=env)

    assert doctor.exit_code(checks) == 0
    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.OK and "anthropic" in provider.detail


def test_exit_0_with_a_live_keyless_daemon(tmp_path):
    """ONB-7 consistency: what chat will do keylessly, doctor must report as ok."""
    snap = _snapshot(tmp_path, ollama=_live("llama3.2"))
    checks = doctor.run_checks(snap, env={})

    assert doctor.exit_code(checks) == 0
    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.OK and "ollama" in provider.detail


def test_exit_0_with_an_active_subscription(tmp_path):
    from lohra.subscription import token_store

    _write_subscription(tmp_path)
    token_store.write_tokens(
        tmp_path,
        token_store.OAuthTokens(access_token="a", refresh_token="r", account_id=None,
                                expires_at=9e9),
    )
    checks = doctor.run_checks(_snapshot(tmp_path), env={})

    assert doctor.exit_code(checks) == 0
    assert _by_name(checks)["subscription"].state == doctor.OK
    assert "subscription" in _by_name(checks)["provider"].detail


def test_subscription_opted_in_but_never_logged_in_is_a_failure(tmp_path):
    """The opt-in record is not a credential: `auth enable` alone cannot answer."""
    _write_subscription(tmp_path)  # no oauth.json, no ~/.codex/auth.json

    checks = doctor.run_checks(_snapshot(tmp_path), env={})
    provider = _by_name(checks)["provider"]

    assert provider.state == doctor.FAIL
    assert "lohra auth login" in provider.remedy
    assert doctor.exit_code(checks) == 2


def test_unsupported_python_warns_and_still_exits_zero(tmp_path):
    """A warn never fails the run: the interpreter is demonstrably executing."""
    env = {"ANTHROPIC_API_KEY": "sk-x"}
    checks = doctor.run_checks(_snapshot(tmp_path, env=env, version=(3, 10, 4)), env=env)

    python = _by_name(checks)["python"]
    assert python.state == doctor.WARN
    assert ">=3.11" in python.detail + python.remedy and "<3.14" in python.detail + python.remedy
    assert doctor.exit_code(checks) == 0


def test_a_selected_provider_without_its_key_fails_with_the_export(tmp_path):
    env = {"LOHRA_PROVIDER": "openai"}
    checks = doctor.run_checks(_snapshot(tmp_path, env=env), env=env)

    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.FAIL
    assert "OPENAI_API_KEY" in provider.remedy and "export" in provider.remedy
    assert doctor.exit_code(checks) == 2


def test_ollama_selected_but_dead_fails_with_the_daemon_command(tmp_path):
    env = {"LOHRA_PROVIDER": "ollama"}
    checks = doctor.run_checks(_snapshot(tmp_path, env=env), env=env)

    provider = _by_name(checks)["provider"]
    assert provider.state == doctor.FAIL
    assert "ollama serve" in provider.remedy
    assert doctor.exit_code(checks) == 2


# --- the individual lines ----------------------------------------------------


def test_a_malformed_mcp_json_warns_and_names_the_file(tmp_path):
    (tmp_path / "mcp.json").write_text("{ oops", encoding="utf-8")

    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))["mcp.json"]

    assert check.state == doctor.WARN
    assert str(tmp_path / "mcp.json") in check.detail + check.remedy


def test_an_absent_optional_file_is_ok_and_silent(tmp_path):
    checks = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))

    for name in ("mcp.json", "cron/jobs.json"):
        assert checks[name].state == doctor.OK
        assert checks[name].remedy == ""
    # tiers ausente é OK mas carrega o CONVITE de primeira classe (o campo
    # remedy, não prosa no detail — achado 8 do review da fatia B).
    assert checks["workflow_tiers.json"].state == doctor.OK
    assert checks["workflow_tiers.json"].remedy == "lohra tiers suggest"


def test_a_missing_workflow_policy_explains_deny_by_default(tmp_path):
    """The dor that produced WF-21: the denial never named the file or the field."""
    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))["workflow_policy.json"]

    assert check.state == doctor.WARN
    assert "workflow_policy.json" in check.remedy
    assert "fs_allow" in check.remedy and "egress_allow" in check.remedy


def test_a_valid_workflow_policy_is_ok(tmp_path):
    (tmp_path / "workflow_policy.json").write_text(
        json.dumps({"fs_allow": ["/tmp"], "egress_allow": []}), encoding="utf-8"
    )
    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}))["workflow_policy.json"]
    assert check.state == doctor.OK


def test_the_profile_cost_divergence_shows_up_as_a_warning(tmp_path):
    """ONB-9(b) again, from the diagnostic side."""
    _write_subscription(tmp_path)
    env = {"LOHRA_PROFILE": "work", "ANTHROPIC_API_KEY": "sk-x"}
    (tmp_path / "profiles" / "work").mkdir(parents=True)

    checks = doctor.run_checks(_snapshot(tmp_path, env=env), env=env)
    check = _by_name(checks)["profile"]

    assert check.state == doctor.WARN
    assert "lohra auth enable --profile work" in check.remedy
    assert doctor.exit_code(checks) == 0  # expensive is not broken


def test_no_profile_warning_without_a_divergence(tmp_path):
    env = {"ANTHROPIC_API_KEY": "sk-x"}
    check = _by_name(doctor.run_checks(_snapshot(tmp_path, env=env), env=env))["profile"]
    assert check.state == doctor.OK and check.remedy == ""


def test_an_expired_own_login_says_to_log_in_again(tmp_path):
    from lohra.subscription import token_store

    _write_subscription(tmp_path)
    token_store.write_tokens(
        tmp_path,
        token_store.OAuthTokens(
            access_token="a", refresh_token="r", account_id=None, expires_at=1000.0
        ),
    )

    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}, now=lambda: 5000.0))["login"]

    assert check.state == doctor.WARN
    assert "lohra auth login" in check.remedy


def test_a_fresh_own_login_is_ok(tmp_path):
    from lohra.subscription import token_store

    token_store.write_tokens(
        tmp_path,
        token_store.OAuthTokens(
            access_token="a", refresh_token="r", account_id=None, expires_at=9000.0
        ),
    )

    check = _by_name(doctor.run_checks(_snapshot(tmp_path), env={}, now=lambda: 5000.0))["login"]
    assert check.state == doctor.OK


def test_harnesses_are_reported_with_the_export_command(tmp_path):
    snap = _snapshot(tmp_path, which=lambda name: f"/usr/bin/{name}")
    check = _by_name(doctor.run_checks(snap, env={}))["harnesses"]

    assert "claude" in check.detail and "codex" in check.detail


def test_doctor_and_init_never_disagree_about_a_keyless_machine(tmp_path):
    """Sibling commands, one truth.

    ONB-7 made a live daemon a real provider for `lohra chat`. If `init`'s report
    still called the same machine "no provider configured" while doctor called it
    ok, the user would get two contradictory answers about one state — exactly the
    "detection predicts, never contradicts" rule fatia A was built on.
    """
    from lohra.onboarding import wizard

    snap = _snapshot(tmp_path, ollama=_live("llama3.2"))

    ready, message = wizard.evaluate(snap, {})
    provider = _by_name(doctor.run_checks(snap, env={}))["provider"]

    assert ready is True and (provider.state == doctor.OK)
    assert "ollama" in message and "no provider configured" not in message


def test_doctor_and_init_agree_when_the_daemon_is_down(tmp_path):
    from lohra.onboarding import wizard

    snap = _snapshot(tmp_path)

    ready, message = wizard.evaluate(snap, {})
    provider = _by_name(doctor.run_checks(snap, env={}))["provider"]

    assert ready is False and provider.state == doctor.FAIL
    assert "no provider configured" in message


# --- --json ------------------------------------------------------------------


def test_json_mode_prints_exactly_one_object_and_no_prose(tmp_path):
    out = io.StringIO()
    code = doctor.run_doctor(out=out, json_output=True, snapshot=_snapshot(tmp_path), env={})

    payload = json.loads(out.getvalue())  # one object, nothing else
    assert payload["exit_code"] == code == 2
    assert payload["ok"] is False
    names = [check["name"] for check in payload["checks"]]
    assert "provider" in names and "python" in names
    assert "environment" in payload  # the ONB-2 snapshot, for scripts


def test_json_and_text_always_agree_on_the_exit_code(tmp_path):
    env = {"ANTHROPIC_API_KEY": "sk-x"}
    snap = _snapshot(tmp_path, env=env)

    text_code = doctor.run_doctor(out=io.StringIO(), snapshot=snap, env=env)
    json_out = io.StringIO()
    json_code = doctor.run_doctor(out=json_out, json_output=True, snapshot=snap, env=env)

    assert text_code == json_code == 0
    assert json.loads(json_out.getvalue())["exit_code"] == 0


# --- the CLI wiring ----------------------------------------------------------


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "lohra"))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
                "GROQ_API_KEY", "TOGETHER_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                "OLLAMA_API_KEY", "LOHRA_PROVIDER", "LOHRA_PROFILE", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "lohra"


def test_the_cli_runs_doctor_headless(isolated, capsys):
    """It is the non-interactive sibling of `init`: no terminal needed, ever."""
    code = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert code == 2  # nothing configured on this isolated machine
    assert "provider" in out and "lohra init" in out


def test_the_cli_doctor_json_is_one_object(isolated, capsys):
    code = cli.main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == code
    assert isinstance(payload["checks"], list)


def test_doctor_exits_zero_once_a_key_is_present(isolated, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    assert cli.main(["doctor"]) == 0
    assert "anthropic" in capsys.readouterr().out
