"""CLI tests for the deterministic (no-network) paths of `lohra chat`.

The real Anthropic round-trip is exercised by the Phase 1 E2E, which needs a
live key; here we pin the resolution and error branches.
"""

import pytest

from lohra import cli


def test_version_flag(capsys):
    # argparse's version action prints and raises SystemExit(0).
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "lohra" in capsys.readouterr().out


def test_no_command_prints_hint(capsys):
    code = cli.main([])
    assert code == 0
    assert "lohra" in capsys.readouterr().out


def test_chat_without_provider_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))  # isolate: no subscription auth.json
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOHRA_PROVIDER", raising=False)
    code = cli.run_chat("oi")
    assert code == 2
    assert "no provider configured" in capsys.readouterr().err


def test_chat_unsupported_api_mode_errors(monkeypatch, capsys, tmp_path):
    # A provider whose api_mode has no transport/client wired must fail cleanly.
    from lohra.providers.base import _REGISTRY, ProviderProfile

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))  # isolate from a real subscription
    profile = ProviderProfile(name="fakeresp", api_mode="weird_mode", env_vars=("FAKE_KEY",))
    monkeypatch.setitem(_REGISTRY, "fakeresp", profile)
    monkeypatch.setenv("FAKE_KEY", "x")
    code = cli.run_chat("oi", provider="fakeresp")
    assert code == 2
    assert "not supported yet" in capsys.readouterr().err


def test_chat_unknown_provider_errors(capsys, monkeypatch, tmp_path):
    # An unknown --provider must fail cleanly (exit 2), not dump a traceback.
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    code = cli.run_chat("oi", provider="totally-bogus")
    assert code == 2
    assert "unknown provider" in capsys.readouterr().err


def _patch_fake_client(monkeypatch, client=None, text="olá do fake"):
    """Replace build_client so the CLI gets a fake instead of a live SDK client."""
    from lohra import agent as agent_pkg

    class FakeAnthropic(agent_pkg.ModelClient):
        def create(self, **kwargs):
            return {
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": None,
            }

    fake = client or FakeAnthropic()
    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def test_chat_success_path_with_injected_client(monkeypatch, capsys, tmp_path):
    """Patch AnthropicClient construction to avoid the SDK/network."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _patch_fake_client(monkeypatch)
    code = cli.run_chat("oi", provider="anthropic", use_tools=False)
    assert code == 0
    assert capsys.readouterr().out.strip() == "olá do fake"


def test_chat_json_emits_valid_envelope(monkeypatch, capsys, tmp_path):
    import json

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _patch_fake_client(monkeypatch)
    code = cli.run_chat("oi", provider="anthropic", use_tools=False, json_output=True)
    assert code == 0
    out = capsys.readouterr().out
    env = json.loads(out)  # stdout is ONLY the JSON (no streamed text)
    assert env["input"] == "oi" and env["output"] == "olá do fake"
    assert env["model"] and "session_id" in env and env["completed"] is True


def test_chat_persists_and_resumes_session(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _patch_fake_client(monkeypatch)

    cli.run_chat("first", provider="anthropic", session="sess-1", use_tools=False)
    cli.run_chat("second", provider="anthropic", session="sess-1", use_tools=False)

    from lohra.memory.paths import state_db_path
    from lohra.state import SessionDB

    db = SessionDB(str(state_db_path()))
    msgs = db.load_messages("sess-1")
    # two turns: user/assistant x2, in order
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "first"
    assert msgs[2]["content"] == "second"
    assert db.get_session("sess-1")["message_count"] == 4
    db.close()


def test_dashboard_without_provider_errors(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))  # isolate: no subscription auth.json
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOHRA_PROVIDER", raising=False)
    manager, app, code = cli.build_dashboard_app(insecure=True)
    assert manager is None and app is None
    assert code == 2
    assert "no provider configured" in capsys.readouterr().err


def test_dashboard_app_builds_with_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # construction is offline
    manager, app, token = cli.build_dashboard_app(insecure=False)
    assert manager is not None and app is not None
    assert token  # a token is generated in secure mode
    app.state.cleanup()  # closes the shared client + db


def test_serve_without_provider_errors(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LOHRA_PROVIDER", raising=False)
    app, code = cli.build_openai_server_app(insecure=True)
    assert app is None
    assert code == 2
    assert "no provider configured" in capsys.readouterr().err


def test_serve_app_builds_with_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # construction is offline
    app, api_key = cli.build_openai_server_app(insecure=False)
    assert app is not None
    assert api_key  # generated in secure mode
    app.state.cleanup()


def test_serve_insecure_has_no_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app, api_key = cli.build_openai_server_app(insecure=True)
    assert app is not None
    assert api_key is None
    app.state.cleanup()


def test_serve_agentic_tools_warn_and_build(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app, _ = cli.build_openai_server_app(insecure=True, tools="read_file,terminal")
    assert app is not None
    err = capsys.readouterr().err
    assert "agentic mode" in err and "read_file" in err  # warns + lists the tools
    app.state.cleanup()


def test_serve_relay_builds_without_tools(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app, _ = cli.build_openai_server_app(insecure=True)  # no --tools
    assert app is not None
    assert "agentic mode" not in capsys.readouterr().err
    app.state.cleanup()


def test_cron_add_list_remove(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_cron("add", name="daily", prompt="summarize", interval=60) == 0
    out = capsys.readouterr().out
    job_id = out.split("added job ")[1].strip()

    assert cli.run_cron("list") == 0
    assert "daily" in capsys.readouterr().out

    assert cli.run_cron("pause", job_id=job_id) == 0
    assert cli.run_cron("remove", job_id=job_id) == 0
    assert cli.run_cron("remove", job_id=job_id) == 1  # already gone


def test_cron_add_without_schedule_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    code = cli.run_cron("add", name="x", prompt="p")  # no --interval/--cron/--at
    assert code == 2
    assert "interval" in capsys.readouterr().err


def test_cron_target_without_id_errors(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_cron("remove") == 2
    assert "needs a job id" in capsys.readouterr().err


def test_cron_list_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_cron("list") == 0
    assert "no scheduled jobs" in capsys.readouterr().out


def test_errored_turn_is_not_persisted(monkeypatch, tmp_path):
    # A turn whose API call fails must not leave a dangling user message that
    # would break alternation on resume.
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    from lohra import agent as agent_pkg

    class BoomClient(agent_pkg.ModelClient):
        def create(self, **kwargs):
            raise RuntimeError("api down")

    _patch_fake_client(monkeypatch, client=BoomClient())
    code = cli.run_chat("hello", provider="anthropic", session="sess-err", use_tools=False)
    assert code == 1

    from lohra.memory.paths import state_db_path
    from lohra.state import SessionDB

    db = SessionDB(str(state_db_path()))
    assert db.load_messages("sess-err") == []  # nothing persisted from the failed turn
    db.close()


# --- profiles (Phase 6: isolated workspaces) ---


def test_profile_create_then_list(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)

    assert cli.main(["profile", "create", "work"]) == 0
    assert (tmp_path / "profiles" / "work" / "memories").is_dir()

    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    assert cli.main(["profile", "list"]) == 0
    assert "work" in capsys.readouterr().out


def test_profile_create_rejects_bad_name(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    code = cli.main(["profile", "create", "../escape"])
    assert code == 2
    assert "invalid profile name" in capsys.readouterr().err


def test_profile_flag_sets_env_before_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    # `chat --profile` must activate the profile; a bad name fails fast with code 2.
    code = cli.main(["chat", "hi", "--profile", "has space"])
    assert code == 2


def test_profile_flag_activates_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    cli.main(["profile", "create", "alice"])
    # after a --profile run the env is set so paths re-root
    cli.main(["chat", "hi", "--profile", "alice", "--no-tools"])
    import os

    assert os.environ.get("LOHRA_PROFILE") == "alice"


def test_out_of_band_bad_profile_fails_cleanly(monkeypatch, tmp_path, capsys):
    # LOHRA_PROFILE set out-of-band (no --profile flag) must fail with exit 2,
    # not a traceback deep in path resolution.
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_PROFILE", "bad name")
    code = cli.main(["chat", "hi", "--no-tools"])
    assert code == 2
    assert "invalid profile name" in capsys.readouterr().err
