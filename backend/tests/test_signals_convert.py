"""Unit da conversão sinal→exceção (issue #40) — sinais DE VERDADE, in-process.

``os.kill(os.getpid(), ...)`` na main thread + um sleep interruptível garante
a entrega determinística (o sinal chega entre bytecodes; sem um ponto de
espera, o raise poderia materializar fora do ``pytest.raises``).
"""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from lohra.agent.signals import (
    TerminatedBySignal,
    defer_signals,
    die_by_signal,
    install_termination_handlers,
    restore_handlers,
)


def _kill_self(signum: int) -> None:
    os.kill(os.getpid(), signum)
    time.sleep(5)  # interrompido pela entrega do sinal — nunca dorme os 5s


def test_sigterm_becomes_exception_and_disarms(monkeypatch):
    previous = install_termination_handlers()
    try:
        with pytest.raises(TerminatedBySignal) as exc:
            _kill_self(signal.SIGTERM)
        assert exc.value.signum == signal.SIGTERM
        # desarme-no-primeiro-sinal: o 2º SIGTERM mataria nativamente
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        restore_handlers(previous)


def test_sig_ign_is_respected(monkeypatch):
    # nohup/daemontools: disposição SIG_IGN nunca é sobrescrita.
    before = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        previous = install_termination_handlers()
        try:
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
            _kill_self(signal.SIGHUP)  # ignorado: não levanta, não mata
        finally:
            restore_handlers(previous)
    finally:
        signal.signal(signal.SIGHUP, before)


def test_install_is_a_noop_off_the_main_thread():
    seen: dict[str, object] = {}

    def worker() -> None:
        seen["previous"] = install_termination_handlers()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen["previous"] == {}


def test_defer_holds_the_signal_until_the_window_closes():
    fired_inside = False
    with pytest.raises(TerminatedBySignal) as exc:
        with defer_signals():
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.05)  # a entrega acontece AQUI — e é engolida (stash)
            fired_inside = True  # a janela completa intacta
    assert fired_inside is True
    assert exc.value.signum == signal.SIGTERM
    # o escape hatch sobrevive à janela: SIG_DFL restaurado pro sinal retido
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def test_defer_converts_sigint_to_keyboard_interrupt():
    with pytest.raises(KeyboardInterrupt):
        with defer_signals():
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
    signal.signal(signal.SIGINT, signal.default_int_handler)


def test_die_by_signal_survives_broken_streams(monkeypatch):
    killed: list[int] = []
    monkeypatch.setattr("lohra.agent.signals.os.kill", lambda pid, sig: killed.append(sig))

    class Broken:
        def flush(self) -> None:
            raise BrokenPipeError("terminal morto (SIGHUP)")

    monkeypatch.setattr("lohra.agent.signals.sys.stdout", Broken())
    monkeypatch.setattr("lohra.agent.signals.sys.stderr", Broken())
    die_by_signal(signal.SIGTERM)  # flush quebrado NÃO rouba o os.kill
    assert killed == [signal.SIGTERM]
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
