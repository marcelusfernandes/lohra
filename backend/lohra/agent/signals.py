"""Morte por sinal vira exceção — o epílogo NORMAL publica o fato (issue #40).

SIGTERM (timeout de harness, kill do operador, shutdown do SO) matava o
interpretador sem executar finally/epílogo: nem dead-turn notice, nem release
do claim, nem envelope — o turno pagou tokens e evaporou. A escolha de design
(verificada adversarialmente) é NUNCA escrever de dentro do handler — um
handler roda entre bytecodes da própria frame interrompida, e um publish
re-entrante na conexão/lock do DurableNoticeStore deadlockaria. Em vez disso:

- ``install_termination_handlers`` converte SIGTERM/SIGHUP em
  ``TerminatedBySignal`` (BaseException: atravessa os ``except Exception`` do
  turno e cai no epílogo, que já sabe publicar/liberar/fechar na ordem certa);
- **desarme-no-primeiro-sinal**: o handler restaura SIG_DFL antes de levantar
  — um SEGUNDO sinal mata nativamente na hora (escape hatch contra epílogo
  travado), sem "force mode";
- **SIG_IGN é respeitado** (regra Unix): ``nohup lohra chat &`` continua
  imune a SIGHUP — não instalamos por cima de uma disposição ignorada;
- ``defer_signals`` protege janelas críticas multi-commit (o fork de
  compactação): o sinal fica retido e re-levanta na SAÍDA da janela, com
  SIG_DFL restaurado antes (o escape hatch sobrevive à janela);
- ``die_by_signal`` re-mata o processo pelo próprio sinal ao fim do epílogo —
  o shell vê WIFSIGNALED/128+N fiel, nunca um exit 1 disfarçado. Flushes são
  best-effort: SIGHUP implica terminal/pipes possivelmente mortos.

SIGKILL/OOM/queda de energia seguem incorrigíveis por definição — o gap
residual (e a alternativa marcador-de-sessão-aberta, prior-art
``workflow_run_locks``/lease) está nomeado na issue #40.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from contextlib import contextmanager
from typing import Iterator

_TERMINATION_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")


class TerminatedBySignal(BaseException):
    """O processo recebeu um sinal de término — BaseException de propósito:
    atravessa todo ``except Exception`` e só o epílogo a trata."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


def _termination_signals() -> list[signal.Signals]:
    return [
        getattr(signal, name)
        for name in _TERMINATION_SIGNAL_NAMES
        if hasattr(signal, name)
    ]


def install_termination_handlers() -> dict[signal.Signals, object]:
    """Instala a conversão sinal→exceção; retorna os handlers anteriores.

    No-op (dict vazio) fora da main thread (``signal.signal`` levanta lá) e
    para sinais com disposição SIG_IGN (nohup/daemontools pediram silêncio).
    """
    if threading.current_thread() is not threading.main_thread():
        return {}

    def _handler(signum: int, _frame: object) -> None:
        # Desarme ANTES de levantar: o 2º sinal mata nativamente, mesmo que a
        # exceção fique presa atrás de uma chamada C bloqueante.
        signal.signal(signum, signal.SIG_DFL)
        raise TerminatedBySignal(signum)

    previous: dict[signal.Signals, object] = {}
    for sig in _termination_signals():
        current = signal.getsignal(sig)
        if current is signal.SIG_IGN:
            continue
        previous[sig] = current
        signal.signal(sig, _handler)
    return previous


def restore_handlers(previous: dict[signal.Signals, object]) -> None:
    """Restaura as disposições anteriores — best-effort (processo one-shot)."""
    if threading.current_thread() is not threading.main_thread():
        return
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)  # type: ignore[arg-type]
        except (ValueError, OSError, TypeError):
            pass


@contextmanager
def defer_signals() -> Iterator[None]:
    """Retém SIGTERM/SIGHUP/SIGINT durante uma janela crítica multi-commit.

    O primeiro sinal retido re-levanta na SAÍDA da janela (SIGINT vira
    ``KeyboardInterrupt``, os demais ``TerminatedBySignal``), com SIG_DFL já
    restaurado para ele — o desarme-no-primeiro-sinal sobrevive à janela.
    No-op fora da main thread.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []
    deferred = _termination_signals() + (
        [signal.SIGINT] if hasattr(signal, "SIGINT") else []
    )
    previous: dict[signal.Signals, object] = {}

    def _stash(signum: int, _frame: object) -> None:
        pending.append(signum)

    for sig in deferred:
        current = signal.getsignal(sig)
        if current is signal.SIG_IGN:
            continue
        previous[sig] = current
        signal.signal(sig, _stash)
    try:
        yield
    finally:
        restore_handlers(previous)
        if pending:
            signum = pending[0]
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
            if signum == getattr(signal, "SIGINT", object()):
                raise KeyboardInterrupt
            raise TerminatedBySignal(signum)


def die_by_signal(signum: int) -> None:
    """Re-mata o processo pelo próprio sinal — exit status fiel (WIFSIGNALED).

    Chamado ao FIM do epílogo, nunca de dentro de um handler. Os flushes são
    best-effort: sob SIGHUP o terminal já pode ter morrido (BrokenPipeError
    aqui não pode roubar o ``os.kill``)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 — pipe morto não muda a morte
            pass
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (ValueError, OSError):
        pass
    os.kill(os.getpid(), signum)
