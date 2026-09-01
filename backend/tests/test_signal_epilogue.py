"""Epílogo sob morte por sinal (issue #40) — sem subprocess.

``run_conversation`` é dublado para levantar ``TerminatedBySignal`` (o que o
handler real faria no próximo bytecode) e ``die_by_signal`` para capturar em
vez de matar — o resto do fluxo é o run_chat REAL, com banco real.
"""

from __future__ import annotations

import json
import signal

import pytest

from lohra.agent.signals import TerminatedBySignal

from tests.test_notice_turn_integration import (
    RecordingClient,
    _patch_fake_client,
    _state_db,
    _text,
)


@pytest.fixture
def dead_by(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr("lohra.agent.signals.die_by_signal", lambda s: killed.append(s))
    return killed


def _raise_signal(monkeypatch, signum):
    import lohra.agent as agent_pkg

    def boom(*args, **kwargs):
        raise TerminatedBySignal(signum)

    monkeypatch.setattr(agent_pkg, "run_conversation", boom)


def test_sigterm_in_flight_publishes_dead_turn_and_dies_faithfully(
    monkeypatch, dead_by, capsys
):
    from lohra.cli import run_chat

    db = _state_db()
    db.create_session("sess-sig", model="m")
    db.close()
    _patch_fake_client(monkeypatch, RecordingClient([_text("ok")]))
    _raise_signal(monkeypatch, signal.SIGTERM)

    code = run_chat("oi", provider="anthropic", session="sess-sig", use_tools=False)

    assert dead_by == [signal.SIGTERM]  # morre pelo PRÓPRIO sinal
    assert code == 128 + signal.SIGTERM  # fallback fiel com die dublado
    db = _state_db()
    token, rows = db.notices.claim("sess-sig")
    assert any("killed (SIGTERM)" in r["text"] for r in rows), rows
    db.notices.release(token)
    assert db.load_messages("sess-sig") == []  # transcript limpo
    db.close()
    assert "session: sess-sig" in capsys.readouterr().err


def test_sigint_under_json_emits_exactly_one_envelope(monkeypatch, dead_by, capsys):
    # Antes do fix: stdout vazio + traceback cru — o contrato --json quebrava.
    from lohra.cli import run_chat

    db = _state_db()
    db.create_session("sess-int", model="m")
    db.close()
    _patch_fake_client(monkeypatch, RecordingClient([_text("ok")]))

    import lohra.agent as agent_pkg

    monkeypatch.setattr(
        agent_pkg,
        "run_conversation",
        lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    code = run_chat(
        "oi", provider="anthropic", session="sess-int", use_tools=False,
        json_output=True,
    )
    assert dead_by == [signal.SIGINT] and code == 128 + signal.SIGINT
    out = capsys.readouterr().out
    envelope = json.loads(out)  # exatamente 1 objeto parseável
    assert envelope["error"] and "SIGINT" in envelope["error"]
    db = _state_db()
    token, rows = db.notices.claim("sess-int")
    assert any("killed (SIGINT)" in r["text"] for r in rows), rows
    db.notices.release(token)
    db.close()


def test_signal_during_ack_never_publishes_a_false_killed_fact(
    monkeypatch, dead_by
):
    """O RED da falha nº1 do painel: sinal DENTRO do _ack_notices, com o turno
    JÁ persistido — 'killed before the turn completed' seria mentira."""
    from lohra.cli import run_chat
    from lohra.state.notices import DurableNoticeStore

    db = _state_db()
    db.create_session("sess-ackwin", model="m")
    db.notices.publish("sess-ackwin", "fato pendente para forçar o claim")
    db.close()
    _patch_fake_client(monkeypatch, RecordingClient([_text("ok")]))

    def ack_killed(self, token, **kwargs):
        raise TerminatedBySignal(signal.SIGTERM)

    monkeypatch.setattr(DurableNoticeStore, "ack", ack_killed)
    code = run_chat("oi", provider="anthropic", session="sess-ackwin", use_tools=False)

    assert dead_by == [signal.SIGTERM] and code == 128 + signal.SIGTERM
    db = _state_db()
    # o turno FOI persistido — e nenhum fato falso 'killed' nasceu
    assert len(db.load_messages("sess-ackwin")) >= 2
    token, rows = db.notices.claim("sess-ackwin")
    assert not any("killed" in r["text"] for r in rows), rows
    if token:
        db.notices.release(token)
    db.close()


def test_main_last_resort_guard_dies_by_the_signal(monkeypatch, dead_by):
    # Sinal que escapa do run_chat (ex.: durante o finally de cleanup) não
    # pode virar traceback + exit 1 — o guard do main morre fiel.
    from lohra import cli

    monkeypatch.setattr(
        cli, "_main", lambda argv=None: (_ for _ in ()).throw(
            TerminatedBySignal(signal.SIGTERM)
        )
    )
    assert cli.main(["chat", "oi"]) == 128 + signal.SIGTERM
    assert dead_by == [signal.SIGTERM]
