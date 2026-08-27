"""Tests for the ~/.lohra/.env loader (hand-rolled, no deps)."""

import os

from lohra.config.env_file import apply_env_file, parse_env_text


def test_parse_basic_pairs():
    text = "ANTHROPIC_API_KEY=sk-ant-123\nOPENAI_API_KEY=sk-oai-456\n"
    assert parse_env_text(text) == {
        "ANTHROPIC_API_KEY": "sk-ant-123",
        "OPENAI_API_KEY": "sk-oai-456",
    }


def test_parse_skips_blanks_and_comments():
    text = "\n# a comment\n  \nKEY=value\n# KEY2=ignored\n"
    assert parse_env_text(text) == {"KEY": "value"}


def test_parse_strips_quotes_and_whitespace():
    text = 'A = "double"\nB = \'single\'\n  C=bare \n'
    assert parse_env_text(text) == {"A": "double", "B": "single", "C": "bare"}


def test_parse_supports_export_prefix():
    assert parse_env_text("export KEY=value\n") == {"KEY": "value"}


def test_parse_value_with_equals_sign():
    # only the first '=' splits key/value
    assert parse_env_text("URL=http://x/?a=1&b=2\n") == {"URL": "http://x/?a=1&b=2"}


def test_parse_ignores_malformed_lines():
    assert parse_env_text("nokey\nKEY=ok\n=noname\n") == {"KEY": "ok"}


def test_apply_sets_missing_vars(tmp_path):
    f = tmp_path / ".env"
    f.write_text("LOHRA_TEST_X=fromfile\n")
    env: dict[str, str] = {}
    applied = apply_env_file(str(f), environ=env)
    assert env["LOHRA_TEST_X"] == "fromfile"
    assert applied == ["LOHRA_TEST_X"]


def test_apply_does_not_override_existing_env(tmp_path):
    f = tmp_path / ".env"
    f.write_text("LOHRA_TEST_X=fromfile\n")
    env = {"LOHRA_TEST_X": "fromenv"}
    applied = apply_env_file(str(f), environ=env)
    assert env["LOHRA_TEST_X"] == "fromenv"  # real env wins
    assert applied == []


def test_apply_missing_file_is_noop(tmp_path):
    applied = apply_env_file(str(tmp_path / "nope.env"), environ={})
    assert applied == []


def test_apply_real_environ_smoke(tmp_path, monkeypatch):
    monkeypatch.delenv("LOHRA_SMOKE_KEY", raising=False)
    f = tmp_path / ".env"
    f.write_text("LOHRA_SMOKE_KEY=yes\n")
    apply_env_file(str(f))
    assert os.environ.get("LOHRA_SMOKE_KEY") == "yes"
