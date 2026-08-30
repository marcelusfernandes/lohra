"""SUP-05 — os DOIS gaps finais do dead-turn / notifier, TDD.

Gap 1 — dead-turn notice DURÁVEL nos descartes "invisíveis" da CLI:

A regra de dead-turn notice (`_publish_dead_turn_notice`) existe no CLI e no
gateway apenas para turno com `error`/`interrupted` (ramo morto explícito).
Mas há DOIS descartes onde o turno é LIMPO (sem erro, sem interrupção) e ainda
assim NADA foi persistido — o transcript canônico fica sem o turno inteiro:

- **falha de persistência** (`db.save_message`/`end_session`/`create_session`
  levanta no meio do bloco de persistência): o turno desaparece e nada diz por
  quê. O claim é liberado no finally (at-least-once), mas o FATO do descarte
  não fica durável — o próximo turno não sabe que o anterior morreu ali.
- **corrida de compactação perdida** (lock de outro processo): o turno é
  descartado de propósito (o outro processo é dono do child canônico), e isso
  também é um fato operacional de continuidade: o turno pagou tokens e não
  pousou em lugar nenhum desta linha.

Em ambos: release do claim (nunca ack), transcript canônico limpo (o turno
não persiste mensagem nenhuma — regra preservada), continuidade best-effort
(publish do notice NUNCA derruba o epílogo do turno), e owner = a sessão
(TTL 24h, texto via `build_turn_notice` — fato operacional, nunca insight).

Gap 2 — `bind_workflow_notifier` no caminho REAL do `run_chat` da CLI:

O dashboard liga o notifier durável (`bind_workflow_notifier(..., db=db)`);
o `run_chat` constrói o `WorkflowService` mas NUNCA liga o notifier — um run
de workflow lançado por um turno da CLI termina sem publicar o summary
durável (nem live: não há inbox no CLI), e a completion notice cross-process
nunca chega ao owner. O wiring deve usar o MESMO db (o `SessionDB` do estado)
e o mesmo contrato do dashboard.
"""

import json

import pytest

from lohra.agent.client import ModelClient
from lohra.state import SessionDB

from tests.test_notice_turn_integration import (
    RecordingClient,
    _patch_fake_client,
    _state_db,
    _text,
)


def _fake_compacted_result():
    return {
        "final_response": "compactei",
        "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "compactei"},
        ],
        "api_calls": 1,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "compacted": True,
        "usage": None,
        "usage_total": None,
        "forced_fallback": False,
        "error_kind": None,
        "retry_after": None,
    }


def _fake_clean_result(text="ok"):
    return {
        "final_response": text,
        "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": text},
        ],
        "api_calls": 1,
        "completed": True,
        "partial": False,
        "interrupted": False,
        "error": None,
        "compacted": False,
        "usage": None,
        "usage_total": None,
        "forced_fallback": False,
        "error_kind": None,
        "retry_after": None,
    }


# --- Gap 1a: falha de persistência em turno LIMPO => dead-turn notice --------


def test_cli_persistence_failure_on_clean_turn_publishes_dead_turn_notice(monkeypatch):
    """`save_message` explode no meio da persistência de um turno LIMPO: além do
    release (já coberto pelas regressões), o fato do descarte fica DURÁVEL —
    o próximo turno/processo precisa saber que o turno anterior morreu ali."""
    from lohra.cli import run_chat
    from lohra.state.db import SessionDB as _SDB

    db = _state_db()
    try:
        db.create_session("sess-persist-dead", model="m")

        client = RecordingClient([_text("ok")])
        _patch_fake_client(monkeypatch, client)

        def broken(self, session_id, message):
            raise RuntimeError("injected save_message failure")

        monkeypatch.setattr(_SDB, "save_message", broken)
        with pytest.raises(RuntimeError):
            run_chat("oi", provider="anthropic", session="sess-persist-dead", use_tools=False)

        # O fato operacional do turno morto está durável (owner = a sessão).
        token, rows = db.notices.claim("sess-persist-dead")
        assert any("turn error" in r["text"] for r in rows), rows
        assert any("injected save_message failure" in r["text"] for r in rows), rows
        db.notices.release(token)
        # Transcript canônico limpo: nenhuma mensagem do turno morto.
        assert db.load_messages("sess-persist-dead") == []
    finally:
        db.close()


def test_cli_persistence_failure_notice_is_available_next_turn(monkeypatch):
    """Continuidade: o notice publicado na falha de persistência entra como
    overlay no turno SEGUINTE — a amnésia não volta por esta fresta."""
    from lohra.cli import run_chat
    from lohra.state.db import SessionDB as _SDB

    db = _state_db()
    try:
        db.create_session("sess-persist-next", model="m")

        client = RecordingClient([_text("ok")])
        _patch_fake_client(monkeypatch, client)

        calls = {"n": 0}

        def flaky_save(self, session_id, message):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first save exploded")

        monkeypatch.setattr(_SDB, "save_message", flaky_save)
        with pytest.raises(RuntimeError):
            run_chat("primeira", provider="anthropic", session="sess-persist-next", use_tools=False)

        monkeypatch.undo()
        client2 = RecordingClient([_text("recuperou")])
        _patch_fake_client(monkeypatch, client2)
        code = run_chat("segunda", provider="anthropic", session="sess-persist-next", use_tools=False)
        assert code == 0

        users = [m for m in client2.calls[0]["messages"] if m["role"] == "user"]
        assert len(users) == 1, "overlay nunca cria user/user"
        assert "first save exploded" in users[0]["content"], users[0]["content"]
        # Transcript canônico sem o overlay.
        assert json.dumps(db.load_messages("sess-persist-next")).find("first save exploded") == -1
        # Turno limpo persistido => pendência zerada (ack pós-persistência).
        assert db.notices.pending_count("sess-persist-next") == 0
    finally:
        db.close()


# --- Gap 1b: corrida de compactação perdida => dead-turn notice --------------


def test_cli_lost_compaction_lock_publishes_dead_turn_notice(monkeypatch):
    """Lock de compactação de outro processo: o turno é descartado (completo,
    sem erro) e o fato fica durável — owner = a sessão, TTL curto, texto
    operacional. O claim NÃO é ackado (o finally libera)."""
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-lock-dead", model="m")
        db.notices.publish("sess-lock-dead", "fact vs lost fork (dead notice)")

        client = RecordingClient([_text("ok")])
        _patch_fake_client(monkeypatch, client)

        import lohra.agent as agent_pkg

        monkeypatch.setattr(
            agent_pkg, "run_conversation", lambda *a, **k: _fake_compacted_result()
        )
        assert db.acquire_compression_lock("sess-lock-dead", "other-process")
        try:
            code = run_chat("oi", provider="anthropic", session="sess-lock-dead", use_tools=False)
            assert code == 0
        finally:
            db.release_compression_lock("sess-lock-dead", "other-process")

        token, rows = db.notices.claim("sess-lock-dead")
        texts = [r["text"] for r in rows]
        # A notice claimada original volta pendente (release, nunca ack)...
        assert any("fact vs lost fork (dead notice)" in t for t in texts)
        # ...e o fato do turno descartado está durável.
        assert any("discarded" in t and "compaction" in t for t in texts), texts
        db.notices.release(token)
        assert db.load_messages("sess-lock-dead") == []
    finally:
        db.close()


def test_cli_won_compaction_lock_still_acks_and_publishes_nothing_extra(monkeypatch):
    """Paridade positiva: vencendo o lock, persistência canônica no child =>
    ack (como já é); e NENHUMA notice operacional extra é publicada — o fato
    do fork limpo não é um dead-turn."""
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-lock-ok", model="m")
        db.create_session("child-ok", model="m", parent_session_id="sess-lock-ok")
        db.notices.publish("sess-lock-ok", "fact across a clean fork")

        client = RecordingClient([_text("ok")])
        _patch_fake_client(monkeypatch, client)

        import lohra.agent as agent_pkg

        monkeypatch.setattr(
            agent_pkg, "run_conversation", lambda *a, **k: _fake_compacted_result()
        )
        code = run_chat("oi", provider="anthropic", session="sess-lock-ok", use_tools=False)
        assert code == 0
        assert db.notices.pending_count("sess-lock-ok") == 0
    finally:
        db.close()


def test_cli_dead_turn_notice_never_breaks_the_epilogue(monkeypatch):
    """Best-effort: um publish de dead-turn notice que explode não pode mascarar
    o erro real nem derrubar o epílogo da CLI."""
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-boom-notice", model="m")

        class BoomClient(ModelClient):
            def create(self, **kwargs):
                raise RuntimeError("provider failure, the real error")

            def stream(self, **kwargs):
                return self.create(**kwargs)

        _patch_fake_client(monkeypatch, BoomClient())

        def exploding_publish(self, owner_id, text, **kwargs):
            raise RuntimeError("notice store exploded")

        from lohra.state.notices import DurableNoticeStore

        monkeypatch.setattr(DurableNoticeStore, "publish", exploding_publish)
        code = run_chat("oi", provider="anthropic", session="sess-boom-notice", use_tools=False)
        assert code == 1  # o erro real do turno, não o do notice
    finally:
        db.close()


# --- Gap 2: bind_workflow_notifier no caminho real do run_chat ---------------


def test_cli_run_chat_binds_the_durable_workflow_notifier(monkeypatch):
    """O `run_chat` liga `bind_workflow_notifier` ao `WorkflowService` real com
    o MESMO db do estado — paridade com o dashboard. Um run OWNED que termina
    publica o summary durável no owner, mesmo com o turno já encerrado."""
    from lohra.cli import run_chat
    from lohra.state.db import SessionDB as _SDB

    real_init = _SDB.__init__
    bound: dict = {}

    import lohra.agent.equip as equip

    original_bind = equip.bind_workflow_notifier

    def spy_bind(service, resolve_inbox, db=None):
        bound["db_class"] = type(db)
        bound["has_notices"] = db is not None and hasattr(db, "notices")
        return original_bind(service, resolve_inbox, db=db)

    monkeypatch.setattr(equip, "bind_workflow_notifier", spy_bind)

    def spy_init(self, path):
        real_init(self, path)

    monkeypatch.setattr(_SDB, "__init__", spy_init)

    client = RecordingClient([_text("ok")])
    _patch_fake_client(monkeypatch, client)
    try:
        code = run_chat("oi", provider="anthropic", use_tools=True)
        assert code == 0
    finally:
        monkeypatch.undo()

    assert bound.get("db_class") is _SDB, (
        "run_chat deve ligar o notifier durável com o SessionDB do estado"
    )
    assert bound.get("has_notices") is True


def test_cli_workflow_completion_lands_as_durable_notice_for_the_next_turn(monkeypatch):
    """E2E do wiring no CLI: um turno com tools lança um workflow OWNED (owner =
    a sessão), o turno termina, e a completion do run fica DURÁVEL no owner —
    disponível para o próximo turno/processo (cross-process completion notice)."""
    from lohra.cli import run_chat

    from lohra.workflow.service import WorkflowService

    started: dict = {}

    real_start = WorkflowService.start

    def spy_start(self, spec, args, **kwargs):
        out = real_start(self, spec, args, **kwargs)
        started.update(out)
        started["owner"] = kwargs.get("owner")
        return out

    monkeypatch.setattr(WorkflowService, "start", spy_start)

    class WorkflowClient(ModelClient):
        """1ª chamada: run_workflow. 2ª: workflow_status wait=true — o turno
        espera o próprio run terminar (a leitura bloqueante da SUP-02), o que
        torna o E2E determinístico: sem a espera, o shutdown do fim do turno
        CANCELA o run em voo e a completion nunca existe. 3ª: texto."""

        def __init__(self):
            self.calls: list[dict] = []
            self.n = 0

        def create(self, **kwargs):
            self.calls.append(kwargs)
            self.n += 1
            if self.n == 1:
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "t1",
                        "name": "run_workflow",
                        "input": {
                            "spec": {
                                "meta": {"name": "cli-notice-demo", "version": 1},
                                "nodes": [
                                    {"id": "a", "type": "agent", "prompt": "go"},
                                ],
                            },
                            "args": {},
                        },
                    }],
                    "stop_reason": "tool_use",
                    "usage": None,
                }
            if self.n == 2:
                return {
                    "content": [{
                        "type": "tool_use",
                        "id": "t2",
                        "name": "workflow_status",
                        "input": {"run_id": started["run_id"], "wait": True},
                    }],
                    "stop_reason": "tool_use",
                    "usage": None,
                }
            return _text("feito")

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            raw = self.create(**kwargs)
            for block in raw.get("content", []):
                if block.get("type") == "text" and on_text:
                    on_text(block["text"])
            return raw

    client = WorkflowClient()
    _patch_fake_client(monkeypatch, client)
    try:
        code = run_chat("rode um workflow", provider="anthropic", use_tools=True)
        assert code == 0
    finally:
        monkeypatch.undo()

    run_id = started.get("run_id")
    assert run_id, "o turno da CLI deve ter lançado o run de workflow"
    owner = started.get("owner")
    assert owner, "o run precisa de owner (a sessão que lançou)"

    # A completion chega como notice DURÁVEL no owner — o fato sobrevive ao
    # processo que encerrou o turno (o db do run_chat já foi fechado aqui).
    reopened = SessionDB(str(_state_db_path()))
    try:
        token, rows = reopened.notices.claim(owner)
        assert token is not None, "completion notice durável ausente para o owner"
        assert any("cli-notice-demo" in r["text"] and "complete" in r["text"] for r in rows), rows
        reopened.notices.release(token)
    finally:
        reopened.close()


def _state_db_path():
    from lohra.memory.paths import state_db_path

    return str(state_db_path())
