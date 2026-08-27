"""Onboarding fatia D — ONB-8: `lohra auth login` absorbs the opt-in.

One intention, one command. Typing `login` already declares "I want to use the
subscription", so the ToS acknowledgement happens *inside* the login instead of
being a separate `enable` the user has to discover first. What must NOT change:

* the opt-in stays **explicit** — the warning is always printed, the ack is
  always recorded, and only the moment moves;
* `lohra auth enable` alone keeps working (the Codex-reuse path has no login,
  and automation depends on `--yes`);
* nothing here may hang: no terminal and no `--yes` is a fast, didactic error.

Determinism rules for this file:

* **No real network.** The device flow is always monkeypatched at the module
  seam (`oauth.start_device_login` / `oauth.poll_for_tokens`); a test that must
  prove the flow was NOT reached installs a raising stub.
* **No real terminal.** Prompts read a fake stdin and write a fake stderr.
  ``builtins.input`` is patched only for the legacy `enable` path, which is
  deliberately left untouched by this slice.
* **No real ``$HOME``.** ``LOHRA_HOME``/``CODEX_HOME`` are pinned to tmp_path.
* **No sleeping.**
"""

from __future__ import annotations

import io
import sys
import time

import pytest

from lohra import cli
from lohra.subscription import manage
from lohra.subscription.credentials import subscription_active


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


USER_CODE = "WXYZ-4321"


@pytest.fixture
def virgin(monkeypatch, tmp_path):
    """A machine with nothing configured: no $HOME, no Codex login, no key."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-none"))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LOHRA_PROVIDER", "LOHRA_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _fake_device_flow(monkeypatch, calls: list[str]):
    """Install a device flow that records its steps and hands back a token."""
    from lohra.subscription import oauth, token_store

    device = oauth.DeviceCode(device_auth_id="dev-1", user_code=USER_CODE, interval=1)

    def _start(post):
        calls.append("start")
        return device

    def _poll(dev, post, **kwargs):
        calls.append("poll")
        return token_store.OAuthTokens(
            access_token="tok", refresh_token="ref", account_id="acct-1",
            expires_at=time.time() + 3600,
        )

    monkeypatch.setattr(oauth, "start_device_login", _start)
    monkeypatch.setattr(oauth, "poll_for_tokens", _poll)
    return device


def _forbid_device_flow(monkeypatch):
    from lohra.subscription import oauth

    def _boom(*args, **kwargs):
        raise AssertionError("the device flow must not start")

    monkeypatch.setattr(oauth, "start_device_login", _boom)
    monkeypatch.setattr(oauth, "poll_for_tokens", _boom)


def _pin_streams(monkeypatch, stdin) -> _FakeTTY:
    err = _FakeTTY()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stderr", err)
    return err


def _logged_in(home) -> bool:
    from lohra.subscription import token_store

    return token_store.read_tokens(home) is not None


# === the headline: one command, one intention ================================


def test_login_on_a_virgin_store_asks_for_the_tos_and_ends_logged_in(monkeypatch, virgin, capsys):
    """`lohra auth login` + one "y" = enabled AND logged in. No `enable` first."""
    calls: list[str] = []
    _fake_device_flow(monkeypatch, calls)
    err = _pin_streams(monkeypatch, _Answers("y"))

    assert cli.run_auth("login") == 0

    text = err.getvalue()
    assert manage.TOS_WARNING in text  # opt-in stays explicit: the warning is shown
    assert "[y/N]" in text  # ...and confirmed, inline
    assert subscription_active(virgin)  # the ack was recorded (same as `enable`)
    assert calls == ["start", "poll"] and _logged_in(virgin)


def test_the_no_browser_fallback_survives_the_new_gate(monkeypatch, virgin):
    """SSH/container: the URL and the code must still be printed to copy by hand."""
    device = _fake_device_flow(monkeypatch, [])
    err = _pin_streams(monkeypatch, _Answers("y"))

    assert cli.run_auth("login") == 0
    text = err.getvalue()
    assert device.verify_url in text and USER_CODE in text


def test_yes_skips_the_question_but_never_the_printed_warning(monkeypatch, virgin):
    """--yes is for automation, not for hiding the risk."""
    calls: list[str] = []
    _fake_device_flow(monkeypatch, calls)
    # A stdin that would raise if read: --yes must never consult it.
    err = _pin_streams(monkeypatch, _Answers(tty=True))

    assert cli.run_auth("login", assume_yes=True) == 0

    text = err.getvalue()
    assert manage.TOS_WARNING in text
    assert "[y/N]" not in text  # no question was asked
    assert subscription_active(virgin) and calls == ["start", "poll"]


def test_declining_the_tos_neither_enables_nor_logs_in(monkeypatch, virgin, capsys):
    _forbid_device_flow(monkeypatch)
    err = _pin_streams(monkeypatch, _Answers("n"))

    assert cli.run_auth("login") != 0
    assert not subscription_active(virgin) and not _logged_in(virgin)
    assert "NOT enabled" in (err.getvalue() + capsys.readouterr().out)


def test_a_bare_enter_declines_because_the_default_is_no(monkeypatch, virgin):
    """Enter always resolves — and for a ToS-gray opt-in it resolves to "no"."""
    _forbid_device_flow(monkeypatch)
    _pin_streams(monkeypatch, _Answers(""))

    assert cli.run_auth("login") != 0
    assert not subscription_active(virgin)


# === headless honesty: refuse, never hang ====================================


def test_without_a_terminal_and_without_yes_login_refuses_didactically(monkeypatch, virgin):
    """A piped stdin cannot say yes — so say what to type instead of waiting."""
    _forbid_device_flow(monkeypatch)
    err = _pin_streams(monkeypatch, _Answers("y", tty=False))

    assert cli.run_auth("login") == 2
    text = err.getvalue()
    assert manage.TOS_WARNING in text  # the risk is stated even when we refuse
    assert "lohra auth login --yes" in text
    assert not subscription_active(virgin) and not _logged_in(virgin)


def test_no_input_still_refuses_before_the_flow_and_writes_no_acknowledgement(
    monkeypatch, virgin
):
    """ONB-5 regression: --no-input must not enable anything on its way out."""
    _forbid_device_flow(monkeypatch)
    err = _pin_streams(monkeypatch, _Answers("y"))

    assert cli.run_auth("login", no_input=True) != 0
    assert not subscription_active(virgin)
    assert "terminal" in err.getvalue()


# === an already-opted-in store is untouched ==================================


def test_an_enabled_store_is_never_asked_again(monkeypatch, virgin):
    """The ack is recorded once. A second login is the plain old device flow —
    including on a pipe, which is how people log in over SSH."""
    manage.enable(virgin)
    calls: list[str] = []
    _fake_device_flow(monkeypatch, calls)
    err = _pin_streams(monkeypatch, _Answers(tty=False))

    assert cli.run_auth("login") == 0
    text = err.getvalue()
    assert manage.TOS_WARNING not in text and "[y/N]" not in text
    assert calls == ["start", "poll"] and _logged_in(virgin)


# === `lohra auth enable` alone: behaviourally identical =======================


def test_enable_alone_still_enables_after_a_confirmation(monkeypatch, virgin, capsys):
    """The Codex-reuse path has no login; `enable` must keep standing alone."""
    _forbid_device_flow(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda: "y")
    err = _pin_streams(monkeypatch, _Answers(tty=False))

    assert cli.run_auth("enable") == 0
    assert subscription_active(virgin) and not _logged_in(virgin)
    assert manage.TOS_WARNING in err.getvalue()
    # The post-enable text now says login alone would have sufficed.
    out = capsys.readouterr().out
    assert "lohra auth login" in out and "codex login" in out


def test_enable_alone_still_aborts_on_anything_but_yes(monkeypatch, virgin, capsys):
    _forbid_device_flow(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda: "")
    _pin_streams(monkeypatch, _Answers(tty=False))

    assert cli.run_auth("enable") == 1
    assert not subscription_active(virgin)
    assert "NOT enabled" in capsys.readouterr().out


def test_enable_with_yes_never_reads_stdin(monkeypatch, virgin):
    def _boom():
        raise AssertionError("--yes must not ask")

    _forbid_device_flow(monkeypatch)
    monkeypatch.setattr("builtins.input", _boom)
    err = _pin_streams(monkeypatch, _Answers(tty=False))

    assert cli.run_auth("enable", assume_yes=True) == 0
    assert subscription_active(virgin) and manage.TOS_WARNING in err.getvalue()


# === the consent gate as a unit ==============================================


def test_the_gate_is_a_no_op_on_an_already_acknowledged_store(tmp_path):
    from lohra.onboarding import auth_login

    manage.enable(tmp_path)
    err = _FakeTTY()
    result = auth_login.ensure_opt_in(tmp_path, stdin=_Answers(tty=False), stderr=err)

    assert result.proceed and result.exit_code == 0
    assert err.getvalue() == ""  # nothing printed: nothing to decide


def test_the_gate_records_the_acknowledgement_before_the_flow_runs(tmp_path):
    """The ack is written by the gate itself, so a failed device flow does not
    lose an acceptance the user already gave."""
    from lohra.onboarding import auth_login

    result = auth_login.ensure_opt_in(tmp_path, stdin=_Answers("yes"), stderr=_FakeTTY())

    assert result.proceed and subscription_active(tmp_path)


def test_the_gate_takes_an_injected_enable_and_activity_check(tmp_path):
    """Both seams are injectable, so the gate is testable without touching disk."""
    from lohra.onboarding import auth_login

    enabled: list[str] = []
    result = auth_login.ensure_opt_in(
        tmp_path,
        stdin=_Answers("y"),
        stderr=_FakeTTY(),
        is_active=lambda home: False,
        enable=lambda home: enabled.append(str(home)),
    )

    assert result.proceed and enabled == [str(tmp_path)]
    assert not subscription_active(tmp_path)  # the real store was not touched


def test_a_stream_that_raises_on_isatty_counts_as_no_terminal(tmp_path):
    class _Rude(io.StringIO):
        def isatty(self):
            raise ValueError("detached")

    from lohra.onboarding import auth_login

    result = auth_login.ensure_opt_in(tmp_path, stdin=_Rude(), stderr=_FakeTTY())
    assert not result.proceed and result.exit_code == 2
    assert not subscription_active(tmp_path)
