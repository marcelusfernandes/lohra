"""SUP-05 integration: DurableNoticeStore wired into GatewaySession and CLI.

Contratos provados aqui (docs/history/2026-08-30-sup-05):

- notices elegíveis (lineage root→tip) entram como request_overlay do turno,
  dentro da user message — nunca como user/user, nunca no system prompt,
  nunca no transcript canônico;
- ack SOMENTE após persistência limpa/canônica (incl. child de compaction);
- erro/interrupção/falha de persistência → release (at-least-once) e
  publicação de notice operacional owner=session_id, TTL 24h;
- o notice do turno falhado está disponível no PRÓXIMO turno.
"""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


class RecordingClient(ModelClient):
    """Fake client that records every kwargs it was called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        raw = self.create(**kwargs)
        for block in raw.get("content", []):
            if block.get("type") == "text" and on_text:
                on_text(block["text"])
        return raw


def _text(text, stop="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop, "usage": None}


class BoomClient(ModelClient):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("injected provider failure")

    def stream(self, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _session(db, client, session_id="s1", **agent_kwargs):
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
        **agent_kwargs,
    )
    if db.get_session(session_id) is None:
        db.create_session(session_id, model=agent.model)
    return GatewaySession(session_id, agent, db)


def _last_user_content(call):
    users = [m for m in call["messages"] if m["role"] == "user"]
    return users[-1]["content"] if users else ""


# --- clean turn: overlay present provider-facing, acked after persist ------


def test_notice_reaches_provider_inside_user_message_and_is_acked(db):
    db.notices.publish("s1", "provider quota nearly exhausted")
    client = RecordingClient([_text("ok")])
    session = _session(db, client)

    frames = []
    session.submit("hello", frames.append)

    assert len(client.calls) == 1
    call = client.calls[0]
    sent = call["messages"]
    users = [m for m in sent if m["role"] == "user"]
    assert len(users) == 1, "overlay must never create a user/user pair"
    assert "hello" in users[0]["content"]
    assert "provider quota nearly exhausted" in users[0]["content"]
    assert "provider quota nearly exhausted" not in call["system"]
    # o transcript canônico NÃO contém o overlay
    msgs = db.load_messages("s1")
    assert json.dumps(msgs).find("quota nearly exhausted") == -1
    # persistiu limpo -> notice acked
    assert db.notices.pending_count("s1") == 0
    complete = frames[-1]["params"]["payload"]
    assert complete["status"] == "complete"


def test_no_notices_means_byte_identical_request(db):
    client = RecordingClient([_text("ok")])
    session = _session(db, client)
    session.submit("hi", lambda _f: None)
    assert client.calls[0]["messages"][-1]["content"] == "hi"


def test_notice_from_parent_lineage_is_claimed_by_child_session(db):
    # parent published, child (created after a fork) runs the next turn
    db.create_session("parent", model="m")
    db.create_session("child", model="m", parent_session_id="parent")
    db.notices.publish("parent", "fact from the parent era")
    client = RecordingClient([_text("ok")])
    session = _session(db, client, session_id="child")

    session.submit("go", lambda _f: None)
    assert "fact from the parent era" in _last_user_content(client.calls[0])
    assert db.notices.pending_count("parent") == 0


# --- erro / interrupção: release + notice operacional ----------------------


def test_error_turn_releases_notice_and_publishes_operational_notice(db):
    db.notices.publish("s1", "stale fact")
    session = _session(db, BoomClient())

    frames = []
    session.submit("hello", frames.append)

    assert frames[-1]["params"]["payload"]["status"] == "error"
    # nada persistido (regra preservada)
    assert db.load_messages("s1") == []
    # a notice original foi LIBERADA (at-least-once), não acked...
    assert db.notices.pending_count("s1") == 2
    # ...e um notice operacional foi publicado (owner = session)
    token, rows = db.notices.claim("s1")
    texts = [r["text"] for r in rows]
    assert any("stale fact" in t for t in texts), "released notice is pending again"
    assert any("injected provider failure" in t for t in texts), texts


def test_failed_turn_notice_is_available_on_the_next_turn(db):
    session = _session(db, BoomClient())
    session.submit("first", lambda _f: None)  # turn 1: error

    client = RecordingClient([_text("recovered")])
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
    )
    session2 = GatewaySession("s1", agent, db)
    session2.submit("second", lambda _f: None)

    overlay = _last_user_content(client.calls[0])
    assert "injected provider failure" in overlay
    assert "second" in overlay
    # após o turno limpo, tudo acked
    assert db.notices.pending_count("s1") == 0


def test_interrupted_turn_releases_and_publishes_operational_notice(db):
    client = RecordingClient([_text("late reply")])
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
    )
    db.create_session("s1", model=agent.model)
    session = GatewaySession("s1", agent, db)
    agent.request_interrupt()  # the turn will be interrupted before the call

    frames = []
    session.submit("hello", frames.append)
    assert frames[-1]["params"]["payload"]["status"] == "interrupted"
    token, rows = db.notices.claim("s1")
    assert rows, "interrupted turn must leave an operational notice"
    assert any("interrupt" in r["text"].lower() for r in rows)


# --- compaction: claim do lineage pai pode ser ackada após o child ---------


class _AlwaysCompress:
    def should_compress(self, *_a):
        return True

    def compress(self, messages, *, summarize):
        return [{"role": "user", "content": "[COMPACTED]"}, *messages]


class _FakeAux:
    def summarizer(self):
        return lambda _t: "SUMMARY"


def test_compacted_turn_acks_lineage_notice_after_clean_child_persist(db):
    db.notices.publish("s1", "fact surviving compaction")
    created = {}

    def on_compaction(parent_id, agent, messages):
        db.create_session("child1", model=agent.model, parent_session_id=parent_id)
        for message in messages:
            db.save_message("child1", message)
        created["done"] = True
        return "child1"

    client = RecordingClient([_text("after compaction")])
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
        context_engine=_AlwaysCompress(),
        aux_client=_FakeAux(),
    )
    db.create_session("s1", model=agent.model)
    session = GatewaySession("s1", agent, db, on_compaction=on_compaction)

    session.submit("hi", lambda _f: None)
    assert created.get("done")
    # persistência limpa no CHILD -> a claim (que cobria o lineage do pai) acka
    assert db.notices.pending_count("s1") == 0
    assert json.dumps(db.load_messages("child1")).find("fact surviving compaction") == -1


# --- falha de persistência: release, nunca ack -----------------------------


def test_save_message_failure_releases_notice(db):
    db.notices.publish("s1", "must survive a broken save")
    client = RecordingClient([_text("ok")])
    session = _session(db, client)

    def broken_save(session_id, message):
        raise RuntimeError("injected save_message failure")

    session.db.save_message = broken_save  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        session.submit("hi", lambda _f: None)

    assert db.notices.pending_count("s1") == 1
    token, rows = db.notices.claim("s1")
    assert any("must survive a broken save" in r["text"] for r in rows)
    db.notices.release(token)


# --- CLI -------------------------------------------------------------------


def _patch_fake_client(monkeypatch, client):
    from lohra import agent as agent_pkg

    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: client)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert issubclass(type(client), agent_pkg.ModelClient)


def _state_db():
    from lohra.memory.paths import state_db_path

    return SessionDB(str(state_db_path()))


def test_cli_notice_overlay_reaches_provider_and_acked_after_persist(monkeypatch):
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-cli", model="claude-opus-4-8")
        db.notices.publish("sess-cli", "cli durable fact")
        client = RecordingClient([_text("olá do fake")])
        _patch_fake_client(monkeypatch, client)

        code = run_chat("oi", provider="anthropic", session="sess-cli", use_tools=False)
        assert code == 0
        assert "cli durable fact" in _last_user_content(client.calls[0])
        users = [m for m in client.calls[0]["messages"] if m["role"] == "user"]
        assert len(users) == 1, "CLI overlay must not create user/user"
        assert db.notices.pending_count("sess-cli") == 0
        # transcript canônico sem overlay
        assert json.dumps(db.load_messages("sess-cli")).find("cli durable fact") == -1
    finally:
        db.close()


def test_cli_failed_turn_notice_available_next_turn(monkeypatch):
    from lohra.cli import run_chat

    db = _state_db()
    try:
        db.create_session("sess-err", model="claude-opus-4-8")

        class Flaky(ModelClient):
            def __init__(self):
                self.calls: list[dict] = []
                self.fail = True

            def create(self, **kwargs):
                self.calls.append(kwargs)
                if self.fail:
                    raise RuntimeError("cli injected failure")
                return _text("recuperou")

            def stream(self, **kwargs):
                return self.create(**kwargs)

        client = Flaky()
        _patch_fake_client(monkeypatch, client)
        code = run_chat("primeira", provider="anthropic", session="sess-err", use_tools=False)
        assert code == 1
        assert db.load_messages("sess-err") == []  # turno com erro não persiste
        assert db.notices.pending_count("sess-err") >= 1

        client.fail = False
        code = run_chat("segunda", provider="anthropic", session="sess-err", use_tools=False)
        assert code == 0
        overlay = _last_user_content(client.calls[-1])
        assert "cli injected failure" in overlay
        assert db.notices.pending_count("sess-err") == 0
    finally:
        db.close()


def test_cli_save_message_failure_releases_notice(monkeypatch):
    from lohra.cli import run_chat
    from lohra.state.db import SessionDB as _SDB

    db = _state_db()
    try:
        db.create_session("sess-save", model="claude-opus-4-8")
        db.notices.publish("sess-save", "fact vs broken save")

        client = RecordingClient([_text("ok")])
        _patch_fake_client(monkeypatch, client)

        original = _SDB.save_message

        def broken(self, session_id, message):
            raise RuntimeError("injected save_message failure")

        monkeypatch.setattr(_SDB, "save_message", broken)
        with pytest.raises(RuntimeError):
            run_chat("oi", provider="anthropic", session="sess-save", use_tools=False)
        monkeypatch.setattr(_SDB, "save_message", original)
        assert db.notices.pending_count("sess-save") == 1, "release, never ack"
        token, rows = db.notices.claim("sess-save")
        assert any("fact vs broken save" in r["text"] for r in rows)
        db.notices.release(token)
    finally:
        db.close()
