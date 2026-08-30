"""Regressões do review crítico (SUP-05 turn-injection).

Cada teste nomeia a falha que teria passado batido sem ele:

R1. O store de notices permanece PURO — sem atributo dinâmico ``db_lineage``;
    o helper de claim recebe os owners explicitamente e a leitura de lineage
    é feita na SessionDB.
R2. O epílogo da CLI nunca referencia ``result`` unbound (exceção antes do
    turno rodar não pode virar NameError no finally) e libera o claim.
R3. "Committed" é separado de "child": fork PERDIDO (lock de compactação de
    outro processo) => o turno não foi persistido => release, NUNCA ack —
    no gateway e no CLI.
R4. Exceção de save_message => release (gateway e CLI).
"""

import pytest

from lohra.agent.client import ModelClient
from lohra.agent.notices_overlay import (
    build_turn_notice,
    claim_lineage_notices,
    format_notice_overlay,
    lineage_owners,
)
from lohra.state import SessionDB

from tests.test_notice_turn_integration import (
    RecordingClient,
    _session,
    _text,
)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


# --- R1: store puro, claim por owners explícitos ----------------------------


def test_notice_store_stays_pure_no_dynamic_lineage(db):
    assert not hasattr(db.notices, "db_lineage"), (
        "o store não pode ganhar acoplamento dinâmico à SessionDB"
    )


def test_lineage_owners_reads_sessiondb_and_filters_empty(db):
    db.create_session("root", model="m")
    db.create_session("mid", model="m", parent_session_id="root")
    db.create_session("tip", model="m", parent_session_id="mid")
    assert lineage_owners(db, "tip") == ["root", "mid", "tip"]
    assert lineage_owners(db, "root") == ["root"]
    assert lineage_owners(db, "ghost") == []


def test_claim_lineage_notices_takes_explicit_owner_list(db):
    db.create_session("root", model="m")
    db.create_session("child", model="m", parent_session_id="root")
    db.notices.publish("root", "fact from the root")
    owners = lineage_owners(db, "child")
    token, rows = claim_lineage_notices(db.notices, owners)
    assert token is not None
    assert [r["text"] for r in rows] == ["fact from the root"]
    db.notices.release(token)


def test_claim_lineage_notices_with_no_owners_never_touches_store():
    # ownerless é recusado ANTES de bater no store (não lança, não claima)
    class Exploding:
        def claim(self, *_a, **_k):
            raise AssertionError("store must not be touched without owners")

    token, rows = claim_lineage_notices(Exploding(), [])
    assert token is None and rows == []


def test_claim_lineage_notices_swallows_store_failure(db):
    class Broken:
        def claim(self, *_a, **_k):
            raise RuntimeError("store locked")

    token, rows = claim_lineage_notices(Broken(), ["s1"])
    assert token is None and rows == []  # turno segue sem overlay (at-least-once)


# --- R2: CLI nunca referencia result unbound --------------------------------


def _state_db():
    from lohra.memory.paths import state_db_path

    return SessionDB(str(state_db_path()))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")


def test_cli_turn_exception_before_result_releases_claim_without_nameerror(monkeypatch):
    """run_conversation explode ANTES de atribuir result: o finally não pode
    levantar NameError consultando result, e o claim volta a pendente."""
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-unbound", model="m")
        db.notices.publish("sess-unbound", "fact before the crash")

        class ExplodingEarly(ModelClient):
            def create(self, **kwargs):
                raise RuntimeError("provider exploded immediately")

            def stream(self, **kwargs):
                return self.create(**kwargs)

        _patch_client(monkeypatch, ExplodingEarly())
        code = run_chat(
            "oi", provider="anthropic", session="sess-unbound", use_tools=False
        )
        assert code == 1
        # release aconteceu (at-least-once) e NÃO houve NameError no finally
        token, rows = db.notices.claim("sess-unbound")
        assert any("fact before the crash" in r["text"] for r in rows)
        db.notices.release(token)
    finally:
        db.close()


def test_cli_save_message_exception_propagates_and_releases(monkeypatch):
    """R4 (CLI): exceção de save_message propaga, o finally libera o claim —
    e NÃO há NameError nem ack."""
    from lohra.cli import run_chat
    from lohra.state.db import SessionDB as _SDB

    db = _state_db()
    try:
        db.create_session("sess-save2", model="m")
        db.notices.publish("sess-save2", "fact vs broken save (cli)")

        client = RecordingClient([_text("ok")])
        _patch_client(monkeypatch, client)

        def broken(self, session_id, messages):
            raise RuntimeError("injected save_message failure")

        monkeypatch.setattr(_SDB, "save_messages", broken)
        with pytest.raises(RuntimeError):
            run_chat("oi", provider="anthropic", session="sess-save2", use_tools=False)

        token, rows = db.notices.claim("sess-save2")
        assert any("fact vs broken save (cli)" in r["text"] for r in rows), (
            "o claim precisa ter sido LIBERADO, não ackado"
        )
        db.notices.release(token)
    finally:
        db.close()


# --- R3: fork perdido (lock de compactação) => release, nunca ack -----------


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


def _compacting_session(db, client, on_compaction):
    from lohra.agent.agent import Agent
    from lohra.gateway.session import GatewaySession
    from lohra.providers import get_provider_profile

    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
        context_engine=_AlwaysCompress(),
        aux_client=_FakeAux(),
    )
    if db.get_session("s1") is None:
        db.create_session("s1", model=agent.model)
    return GatewaySession("s1", agent, db, on_compaction=on_compaction)


class _AlwaysCompress:
    def should_compress(self, *_a):
        return True

    def compress(self, messages, *, summarize):
        return messages


class _FakeAux:
    def summarizer(self):
        return lambda _t: "SUMMARY"


def test_gateway_lost_fork_releases_notice_never_acks(db):
    db.notices.publish("s1", "fact across a lost fork")
    client = RecordingClient([_text("after compaction")])
    session = _compacting_session(db, client, lambda p, a, m: None)  # lock perdido

    frames = []
    session.submit("hi", frames.append)
    assert frames[-1]["params"]["payload"]["status"] == "complete"
    assert db.load_messages("s1") == []  # nada persistido
    token, rows = db.notices.claim("s1")
    assert any("fact across a lost fork" in r["text"] for r in rows), (
        "fork perdido => o turno não foi persistido => release, NUNCA ack"
    )
    db.notices.release(token)


def test_gateway_clean_fork_acks_notice(db):
    db.create_session("child_ok", model="m", parent_session_id="s1")
    db.notices.publish("s1", "fact across a clean fork")

    def fork(parent_id, agent, messages):
        for message in messages:
            db.save_message("child_ok", message)
        return "child_ok"

    client = RecordingClient([_text("after compaction")])
    session = _compacting_session(db, client, fork)

    frames = []
    session.submit("hi", frames.append)
    assert frames[-1]["params"]["payload"]["status"] == "complete"
    assert db.notices.pending_count("s1") == 0, "persistência limpa no child => ack"


def test_cli_lost_compaction_lock_releases_notice_never_acks(monkeypatch):
    """R3 (CLI): o lock de compactação é de outro processo => o turno não é
    persistido => as notices NÃO ackam (o finally libera)."""
    from lohra.cli import run_chat
    from lohra.state import compression_lock as _unused  # noqa: F401

    db = _state_db()
    try:
        db.create_session("sess-lock", model="m")
        db.notices.publish("sess-lock", "fact vs lost fork")

        client = RecordingClient([_text("ok")])
        _patch_client(monkeypatch, client)

        import lohra.agent as agent_pkg

        monkeypatch.setattr(
            agent_pkg, "run_conversation", lambda *a, **k: _fake_compacted_result()
        )
        # o lock JÁ é de outro processo:
        assert db.acquire_compression_lock("sess-lock", "other-process")
        try:
            code = run_chat(
                "oi", provider="anthropic", session="sess-lock", use_tools=False
            )
            assert code == 0
        finally:
            db.release_compression_lock("sess-lock", "other-process")

        token, rows = db.notices.claim("sess-lock")
        assert any("fact vs lost fork" in r["text"] for r in rows), (
            "lock perdido => release, NUNCA ack"
        )
        db.notices.release(token)
    finally:
        db.close()


def test_cli_won_compaction_lock_acks_notice(monkeypatch):
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-lock2", model="m")
        db.notices.publish("sess-lock2", "fact across a cli fork")

        client = RecordingClient([_text("ok")])
        _patch_client(monkeypatch, client)

        import lohra.agent as agent_pkg

        monkeypatch.setattr(
            agent_pkg, "run_conversation", lambda *a, **k: _fake_compacted_result()
        )
        code = run_chat("oi", provider="anthropic", session="sess-lock2", use_tools=False)
        assert code == 0
        assert db.notices.pending_count("sess-lock2") == 0, (
            "persistência canônica no child => ack"
        )
    finally:
        db.close()


# --- R4 (gateway): exceção de save_message => release -----------------------


def test_gateway_save_message_exception_releases_and_propagates(db):
    db.notices.publish("s1", "fact vs broken save (gateway)")
    client = RecordingClient([_text("ok")])
    session = _session(db, client)

    def broken_save(session_id, messages):
        raise RuntimeError("injected save_message failure")

    session.db.save_messages = broken_save  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        session.submit("hi", lambda _f: None)

    token, rows = db.notices.claim("s1")
    assert any("fact vs broken save (gateway)" in r["text"] for r in rows), (
        "save_message quebrado => release, NUNCA ack"
    )
    db.notices.release(token)


# --- sanidade do formato/notice ---------------------------------------------


def test_build_turn_notice_includes_kind_and_error_bounded():
    text = build_turn_notice(
        status="error", error="boom " * 200, error_kind="quota_exhausted"
    )
    assert "error_kind=quota_exhausted" in text
    assert "error=" in text
    assert len(text) <= 500


def test_format_notice_overlay_none_when_no_rows():
    assert format_notice_overlay([]) is None
