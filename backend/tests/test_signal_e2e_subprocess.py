"""E2E da issue #40 — sinais DE VERDADE em subprocessos reais.

Cada caso sobe um ``lohra`` real (``cli.run_chat`` num ``sys.executable``
filho, provider dublado no módulo-fonte ``lohra.agent.client``), espera o
sinal de prontidão no stderr e manda o sinal com ``os.kill``. Os asserts são
do harness, sobre o exit status REAL (``Popen.returncode`` negativo =
WIFSIGNALED — o shell veria 128+N) e sobre o banco reaberto.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from lohra.state import SessionDB

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="sinais POSIX")

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

_CHILD = textwrap.dedent(
    """
    import os, sys, time
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")

    import lohra.agent.client as clientmod
    from lohra.agent.client import ModelClient

    MODE = sys.argv[1]  # "provider" | "tool"
    JSON = sys.argv[2] == "json"

    def _text(text):
        return {"content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn", "usage": None}

    class Scripted(ModelClient):
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if MODE == "provider":
                print("READY", file=sys.stderr, flush=True)
                time.sleep(3600)  # o sinal chega AQUI, no meio da chamada
            if self.calls == 1 and MODE == "tool":  # UMA tool bloqueante
                print("READY", file=sys.stderr, flush=True)
                return {"content": [
                    {"type": "tool_use", "id": "t1", "name": "terminal",
                     "input": {"command": "sleep 3600"}}],
                    "stop_reason": "tool_use", "usage": None}
            if self.calls == 1 and MODE == "tools2":  # DUAS em paralelo (pool)
                print("READY", file=sys.stderr, flush=True)
                return {"content": [
                    {"type": "tool_use", "id": "t1", "name": "terminal",
                     "input": {"command": "sleep 3600"}},
                    {"type": "tool_use", "id": "t2", "name": "terminal",
                     "input": {"command": "echo rapida"}}],
                    "stop_reason": "tool_use", "usage": None}
            return _text("nunca chega")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    clientmod.build_client = lambda profile, **kw: Scripted()

    from lohra import cli
    code = cli.run_chat(
        "oi", provider="anthropic", session="sess-kill",
        use_tools=(MODE in ("tool", "tools2")), yolo=True, json_output=JSON,
        no_input=True,
    )
    sys.exit(code)  # inalcançável no caminho de sinal (die_by_signal mata antes)
    """
)


def _spawn(tmp_path, mode: str, out_mode: str) -> subprocess.Popen:
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in {"LOHRA_HOME", "LOHRA_PROFILE"}}
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["LOHRA_HOME"] = str(home)
    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(script), mode, out_mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(BACKEND_ROOT),
    )


def _wait_ready(proc: subprocess.Popen, deadline: float = 60.0) -> None:
    # select antes do readline: um filho pendurado em silêncio deve virar
    # FALHA no deadline, nunca hang infinito do teste (finding do review).
    import select

    start = time.monotonic()
    while time.monotonic() - start < deadline:
        remaining = deadline - (time.monotonic() - start)
        readable, _, _ = select.select([proc.stderr], [], [], min(remaining, 1.0))
        if not readable:
            if proc.poll() is not None:
                break
            continue
        line = proc.stderr.readline()
        if not line and proc.poll() is not None:
            break
        if "READY" in line:
            return
    raise AssertionError(f"child never became ready (rc={proc.poll()})")


def _dead_turn_rows(tmp_path):
    db = SessionDB(str(tmp_path / "home" / "state.db"))
    try:
        token, rows = db.notices.claim("sess-kill")
        if token:
            db.notices.release(token)
        transcript = db.load_messages("sess-kill")
        return rows, transcript
    finally:
        db.close()


def test_sigterm_mid_provider_call_publishes_notice_and_dies_faithfully(tmp_path):
    proc = _spawn(tmp_path, "provider", "plain")
    try:
        _wait_ready(proc)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGTERM  # WIFSIGNALED fiel (shell: 143)
    stderr = proc.stderr.read() or ""
    assert "Traceback" not in stderr  # morte limpa, nunca traceback cru
    rows, transcript = _dead_turn_rows(tmp_path)
    assert any("killed (SIGTERM)" in r["text"] for r in rows), rows
    assert transcript == []  # o turno em voo nunca persistiu


def test_sigterm_mid_tool_call_same_guarantees_and_db_intact(tmp_path):
    # AC #2 da issue: sinal no meio de uma TOOL rodando — o handler não pode
    # corromper o banco nem travar o processo.
    proc = _spawn(tmp_path, "tool", "plain")
    try:
        _wait_ready(proc)
        time.sleep(1.0)  # a tool (sleep 3600) chega a estar em execução
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGTERM
    rows, transcript = _dead_turn_rows(tmp_path)  # o DB reabre íntegro
    assert any("killed (SIGTERM)" in r["text"] for r in rows), rows
    assert transcript == []


def test_sigterm_with_parallel_tool_calls_dies_promptly(tmp_path):
    """Finding HIGH do review: com >=2 tools no pool, o __exit__ do
    ThreadPoolExecutor joinava os workers vivos — o epílogo só rodava quando a
    tool mais lenta terminasse, e o grace de um supervisor (10-12s) estourava
    antes: SIGKILL sem notice. A morte tem que ser PRONTA."""
    proc = _spawn(tmp_path, "tools2", "plain")
    try:
        _wait_ready(proc)
        time.sleep(1.0)  # as duas tools chegam a estar no pool
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=60)
        elapsed = time.monotonic() - t0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGTERM
    assert elapsed < 10, f"unwinding esperou a tool lenta ({elapsed:.1f}s)"
    rows, transcript = _dead_turn_rows(tmp_path)
    assert any("killed (SIGTERM)" in r["text"] for r in rows), rows
    assert transcript == []


def test_sigint_under_json_still_emits_exactly_one_envelope(tmp_path):
    # Antes do fix: stdout vazio + traceback — o contrato --json quebrava.
    proc = _spawn(tmp_path, "provider", "json")
    try:
        _wait_ready(proc)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGINT
    envelope = json.loads(proc.stdout.read())  # exatamente 1 objeto parseável
    assert envelope["error"] and "SIGINT" in envelope["error"]
    rows, _ = _dead_turn_rows(tmp_path)
    assert any("killed (SIGINT)" in r["text"] for r in rows), rows
