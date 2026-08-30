"""SUP-05 E2E — a cadeia real de recuperação cross-process com SUBPROCESSOS.

Diferença dos testes irmãos (``test_workflow_recovery_notice``,
``test_notice_turn_integration``): aqui cada "processo" é um subprocesso Python
REAL (``sys.executable``), sobre UM SessionDB file-backed — nada compartilha
memória, exatamente como um crash de verdade:

- **Processo A** cria um run ``running`` (owner ``sess-1``), sinaliza que a
  célula bloqueada INICIOU (segunda linha no stdout) e morre SEM shutdown: o
  harness só o mata depois de ler o sinal; o lease fica no SQLite e expira
  sozinho (``lease_ttl=1.0`` real);
- **Processo B** reabre o DB, vê o run órfão (lease morto), resume-o e publica
  a recovery notice DURÁVEL para o owner ANTERIOR (``sess-1``);
- o HARNESS reabre o DB e prova que a notice existe cross-process (EXATAMENTE
  UMA row durável global);
- **Processo C** (nova sessão, nova conexão) consome a notice no próximo turno
  via overlay request-only (claim_lineage_notices + run_conversation) e faz ack.

Asserts (todos no harness, sobre stdout JSON dos subprocessos):
- a recovery notice existiu durable cross-process: EXATAMENTE UMA row na tabela
  ``durable_notices`` INTEIRA (owner certo, texto do run certo — nada além:
  nem duplicata, nem broadcast para outro owner);
- o overlay chegou EXATAMENTE UMA VEZ provider-facing (dentro da única user
  message, ``run_id`` aparece 1x);
- o system prompt congelado é BYTE-IDÊNTICO: o processo C captura o snapshot do
  agent (``agent.system_prompt().text``) E a coluna persistida
  (``sessions.system_prompt``) antes E depois do turno, e o harness compara
  ambos, exatamente, com o ``system`` realmente enviado ao provider;
- o transcript canônico contém SOMENTE a user message real (sem notice extra);
- o ack zerou a pendência.

Custo de relógio: só a expiração do lease do processo A usa wall-clock real
(``lease_ttl=1.0`` + um sleep determinístico de ~1.5s no harness); todo o resto
é determinístico.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import time

from lohra.state import SessionDB

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

# --- base compartilhada pelos três subprocessos ------------------------------

_SHARED = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path

    from lohra.agent.agent import Agent
    from lohra.agent.client import ModelClient
    from lohra.providers import get_provider_profile
    from lohra.state import SessionDB

    DB_PATH, HOME = sys.argv[1], sys.argv[2]
    TWO_NODE = {
        "meta": {"name": "demo", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "then ${a}"},
        ],
    }

    def _text_response(text):
        return {
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }

    class ScriptedClient(ModelClient):
        def __init__(self, responder):
            self._responder = responder

        def create(self, **kwargs):
            return _text_response(self._responder(kwargs))

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    def _fail(payload):
        print(json.dumps(payload), flush=True)
        sys.exit(1)
    """
)

# --- processo A: cria o run running e morre SEM shutdown (o harness mata) ----

_PHASE_A = _SHARED + textwrap.dedent(
    """
    import threading

    from lohra.workflow.service import WorkflowService

    db = SessionDB(DB_PATH)
    started = threading.Event()

    def blocked(_kwargs):
        started.set()
        time.sleep(3600)  # o run fica preso: o processo morre dentro dele

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(blocked),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=Path(HOME), lease_ttl=1.0)
    out = svc.start(TWO_NODE, {}, owner="sess-1")
    if "error" in out:
        _fail(out)
    # Linha 1: o run_id. Linha 2: só depois que a célula bloqueada INICIOU —
    # o harness só mata o processo após ler este sinal.
    print(out["run_id"], flush=True)
    if not started.wait(30):
        _fail({"error": "blocked leaf never started"})
    print("blocked-started", flush=True)
    time.sleep(3600)  # morre SEM shutdown (o harness manda kill)
    """
)

# --- processo B: reabre o DB, recupera o run órfão e publica a notice --------

_PHASE_B = _SHARED + textwrap.dedent(
    """
    from lohra.workflow.service import WorkflowService

    run_id = sys.argv[3]
    db = SessionDB(DB_PATH)

    def responder(_kwargs):
        return "R"

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=Path(HOME), lease_ttl=1.0)
    out = svc.start(resume_run_id=run_id, owner="sess-2")
    if "error" in out:
        _fail(out)
    rollup = svc.status(run_id, wait=True, timeout=30)
    svc.shutdown()
    db.close()
    print(json.dumps({"status": rollup.get("status")}), flush=True)
    """
)

# --- processo C: nova sessão reabre o DB e consome a notice no próximo turno -

_PHASE_C = _SHARED + textwrap.dedent(
    """
    from lohra.gateway.session import GatewaySession

    run_id = sys.argv[3]
    db = SessionDB(DB_PATH)  # conexão NOVA: nada herdado do processo B

    pre_pending = db.notices.pending_count("sess-1")

    class RecordingClient(ModelClient):
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return _text_response("ok")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            raw = self.create(**kwargs)
            for block in raw.get("content", []):
                if block.get("type") == "text" and on_text:
                    on_text(block["text"])
            return raw

    client = RecordingClient()
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
    )

    # Prova byte-idêntica do system prompt congelado, nos DOIS lugares onde ele
    # vive: o snapshot em memória do agent E a coluna persistida na tabela
    # sessions (mesmo caminho do manager/CLI: create_session com o texto do
    # snapshot). Captura ANTES do turno (constrói e congela o cache) e DEPOIS.
    system_before = agent.system_prompt().text
    db.create_session("sess-1", model=agent.model, system_prompt=system_before)
    session_before = db.get_session("sess-1")["system_prompt"]
    if session_before != system_before:
        _fail({"error": "sessions.system_prompt divergiu do snapshot do agent"})

    session = GatewaySession("sess-1", agent, db)
    session.submit("voltando", lambda _frame: None)

    system_after = agent.system_prompt().text
    session_after = db.get_session("sess-1")["system_prompt"]
    print(
        json.dumps(
            {
                "pre": pre_pending,
                "ncalls": len(client.calls),
                "call": client.calls[0],
                "transcript": db.load_messages("sess-1"),
                "post": db.notices.pending_count("sess-1"),
                "system_before": system_before,
                "system_after": system_after,
                "session_before": session_before,
                "session_after": session_after,
                "cache_frozen": agent._cached_system_prompt is not None,
            },
            default=str,
        ),
        flush=True,
    )
    """
)


def _write_phase(tmp_path: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _subprocess_env(home: pathlib.Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"LOHRA_HOME", "LOHRA_PROFILE"}}
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    env["LOHRA_HOME"] = str(home)  # custo de sessão/estado fora do ~/.lohra real
    return env


def _run_phase(path: pathlib.Path, db_path: pathlib.Path, home: pathlib.Path, *args: str):
    return subprocess.run(  # noqa: S603
        [sys.executable, str(path), str(db_path), str(home), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=_subprocess_env(home),
        cwd=str(BACKEND_ROOT),
    )


def _die(proc: subprocess.Popen, context: str) -> str:
    """Coleta stdout+stderr de um subprocesso morto para a mensagem de erro."""
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    err = (proc.stderr.read() or "").strip()
    out = (proc.stdout.read() or "").strip()
    return f"{context}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


# --- o teste -----------------------------------------------------------------


def test_sup05_full_chain_across_three_real_subprocesses(tmp_path):
    db_path = tmp_path / "state.db"
    home = tmp_path / "home"
    home.mkdir()

    script_a = _write_phase(tmp_path, "phase_a.py", _PHASE_A)
    script_b = _write_phase(tmp_path, "phase_b.py", _PHASE_B)
    script_c = _write_phase(tmp_path, "phase_c.py", _PHASE_C)
    env = _subprocess_env(home)

    # --- processo A: cria o run running e morre SEM shutdown (kill) ---------
    proc_a = subprocess.Popen(  # noqa: S603
        [sys.executable, str(script_a), str(db_path), str(home)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(BACKEND_ROOT),
    )
    try:
        run_id = proc_a.stdout.readline().strip()
        assert run_id and " " not in run_id, _die(proc_a, "processo A não criou o run")
        signal = proc_a.stdout.readline().strip()  # a célula bloqueada INICIOU
        assert signal == "blocked-started", _die(proc_a, "célula bloqueada não iniciou")
    finally:
        proc_a.kill()  # morte abrupta: lease fica no SQLite, ninguém renova
        proc_a.wait(timeout=10)

    # O lease expira sozinho (ttl=1.0s, wall-clock real dentro do subprocesso).
    time.sleep(1.5)

    # --- processo B: reabre o DB e recupera o run órfão ---------------------
    out_b = _run_phase(script_b, db_path, home, run_id)
    assert out_b.returncode == 0, (
        f"processo B falhou\n--- stdout ---\n{out_b.stdout}\n--- stderr ---\n{out_b.stderr}"
    )
    assert json.loads(out_b.stdout)["status"] == "complete"

    # --- o harness prova a EXISTÊNCIA DURÁVEL cross-process da notice -------
    harness = SessionDB(str(db_path))
    try:
        rows = harness.notices._connection.execute(
            "SELECT owner_id, text FROM durable_notices"
        ).fetchall()
        # EXATAMENTE UMA row durável GLOBAL: a recuperação publicou um fato e
        # nada mais — nem duplicata, nem broadcast para outro owner.
        assert len(rows) == 1, f"expected exactly one durable notice, got {rows!r}"
        row = rows[0]
        assert row["owner_id"] == "sess-1", "o fato é do owner ANTERIOR, nunca do resumer"
        assert run_id in row["text"]
        assert "stopped" in row["text"] and "replayed" in row["text"] and "lost" in row["text"]
    finally:
        harness.close()

    # --- processo C: nova sessão reabre o DB e consome no próximo turno -----
    out_c = _run_phase(script_c, db_path, home, run_id)
    assert out_c.returncode == 0, (
        f"processo C falhou\n--- stdout ---\n{out_c.stdout}\n--- stderr ---\n{out_c.stderr}"
    )
    payload = json.loads(out_c.stdout)

    # a notice existia ANTES do turno, durable, vista pela conexão nova
    assert payload["pre"] == 1
    assert payload["ncalls"] == 1, "exatamente uma chamada provider-facing"

    call = payload["call"]
    users = [m for m in call["messages"] if m.get("role") == "user"]
    assert len(users) == 1, "overlay nunca cria user/user"
    content = users[0]["content"]
    assert "voltando" in content, "a user message real está lá"
    assert content.count(run_id) == 1, "overlay provider-facing EXATAMENTE uma vez"
    assert content.count("AVISOS OPERACIONAIS") == 1

    # System prompt congelado BYTE-IDÊNTICO em ambos os registros: o snapshot em
    # memória do agent E a coluna persistida sessions.system_prompt são iguais
    # antes e depois do turno — e EXATAMENTE o `system` enviado ao provider.
    assert payload["system_before"] == payload["system_after"], (
        "o system prompt congelado (agent) mudou durante o turno"
    )
    assert payload["session_before"] == payload["session_after"], (
        "a coluna sessions.system_prompt mudou durante o turno"
    )
    assert call["system"] == payload["system_after"], (
        "o system enviado ao provider não é o snapshot congelado do agent"
    )
    assert call["system"] == payload["session_after"], (
        "o system enviado ao provider não é o persistido em sessions.system_prompt"
    )

    transcript = payload["transcript"]
    assert [m["role"] for m in transcript] == ["user", "assistant"]
    assert transcript[0]["content"] == "voltando", "só a user message REAL no canônico"
    assert json.dumps(transcript).find(run_id) == -1, "nenhuma notice extra persistida"

    # ack pós-persistência canônica: a pendência zerou
    assert payload["post"] == 0
