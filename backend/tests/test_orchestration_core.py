"""Tests for OrchestrationCore — spawn/steer/collect/cancel + concurrency cap."""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from tests.test_loop import FakeClient, _text_response, _tool_call_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _child_factory(responses_per_child, **agent_overrides):
    """A factory yielding a fresh isolated agent per call, each programmed with
    its own response list (popped in spawn order)."""
    queue = list(responses_per_child)

    def factory() -> Agent:
        responses = queue.pop(0) if queue else [_text_response("ok")]
        kwargs = dict(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(responses),
        )
        kwargs.update(agent_overrides)
        return Agent(**kwargs)

    return factory


def _core(db, responses_per_child, **kwargs):
    return OrchestrationCore(db, _child_factory(responses_per_child), **kwargs)


def test_spawn_returns_id_and_collect_waits(db):
    core = _core(db, [[_text_response("hello from child")]])
    try:
        sub_id = core.spawn("do a thing")
        assert isinstance(sub_id, str) and len(sub_id) > 0
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["output"] == "hello from child"
    finally:
        core.shutdown()


def test_persists_with_parent_id(db):
    core = _core(db, [[_text_response("x")]])
    try:
        sub_id = core.spawn("task", parent_id="parent-123")
        core.collect(sub_id, wait=True, timeout=5)
        row = db.get_session(sub_id)
        assert row is not None
        assert row["parent_session_id"] == "parent-123"
        assert row["source"] == "orchestration"
    finally:
        core.shutdown()


def test_concurrency_cap_runs_all_work(db):
    # cap of 2, five sub-sessions — the excess queues but nothing is dropped.
    core = _core(db, [[_text_response(f"r{i}")] for i in range(5)], max_concurrent=2)
    try:
        ids = [core.spawn(f"task {i}") for i in range(5)]
        outputs = {core.collect(i, wait=True, timeout=5)["output"] for i in ids}
        assert outputs == {f"r{i}" for i in range(5)}
    finally:
        core.shutdown()


def test_steer_idle_runs_a_followup_turn(db):
    # turn 1 -> "first"; after it completes, steer starts turn 2 -> "second".
    core = _core(db, [[_text_response("first"), _text_response("second")]])
    try:
        sub_id = core.spawn("start")
        assert core.collect(sub_id, wait=True, timeout=5)["output"] == "first"
        steer = core.steer(sub_id, "now do the second thing")
        assert steer == {"ok": True, "queued": False}
        assert core.collect(sub_id, wait=True, timeout=5)["output"] == "second"
    finally:
        core.shutdown()


def test_steer_while_busy_queues_to_inbox(db):
    # A gated client: the turn does a tool call (iter 1), then blocks on iter 2's
    # call until released — so we can steer while it's provably busy.
    gate = threading.Event()
    started = threading.Event()

    class GatedClient(FakeClient):
        def create(self, **kwargs):
            if self.calls:  # second call onward — block until released
                started.set()
                gate.wait(5)
            return super().create(**kwargs)

    def factory():
        # 3rd response: the steer lands after iter-2's drain, so the turn ends and
        # the core runs a FOLLOW-UP turn for the stranded steer (no lost steer).
        client = GatedClient(
            [
                _tool_call_response([("c1", "noop", {})]),
                _text_response("finished"),
                _text_response("handled the steer"),
            ]
        )
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
            tool_dispatch=lambda name, args: "{}",
        )

    core = OrchestrationCore(db, factory)
    try:
        sub_id = core.spawn("start")
        assert started.wait(5)  # the turn is now blocked mid-flight (busy)
        steer = core.steer(sub_id, "extra instruction")
        assert steer == {"ok": True, "queued": True}  # routed to inbox, not a new turn
        gate.set()  # release the turn
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["output"] == "handled the steer"  # the stranded steer was processed
    finally:
        gate.set()
        core.shutdown()


def test_list_children_by_parent(db):
    core = _core(db, [[_text_response("a")], [_text_response("b")], [_text_response("c")]])
    try:
        a = core.spawn("1", parent_id="P")
        b = core.spawn("2", parent_id="P")
        c = core.spawn("3", parent_id="Q")
        for i in (a, b, c):
            core.collect(i, wait=True, timeout=5)
        assert set(core.list_children("P")) == {a, b}
        assert core.list_children("Q") == [c]
    finally:
        core.shutdown()


def test_registry_evicts_oldest_terminal_subsessions(db):
    core = _core(db, [[_text_response(f"r{i}")] for i in range(5)], max_children=3)
    try:
        ids = []
        for _ in range(5):
            sub_id = core.spawn("task")
            core.collect(sub_id, wait=True, timeout=5)  # make it terminal before the next spawn
            ids.append(sub_id)
        # only the most recent 3 survive in memory; the oldest 2 were evicted
        survivors = [i for i in ids if "error" not in core.collect(i)]
        assert len(survivors) == 3
        assert survivors == ids[-3:]
        # an evicted child can no longer be collected (DB row persists, memory dropped)
        assert "error" in core.collect(ids[0])
    finally:
        core.shutdown()


def test_running_subsessions_are_never_evicted(db):
    gate = threading.Event()
    started = threading.Event()

    class GatedClient(FakeClient):
        def create(self, **kwargs):
            started.set()
            gate.wait(5)
            return super().create(**kwargs)

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=GatedClient([_text_response("done")]),
        )

    core = OrchestrationCore(db, factory, max_children=1)
    try:
        running = core.spawn("long")
        assert started.wait(5)  # it's running, registry at cap (1)
        # spawning again can't evict the running one — registry exceeds cap, no crash
        second = core.spawn("another")
        assert "error" not in core.collect(running)  # the running child still tracked
        assert "error" not in core.collect(second)
    finally:
        gate.set()
        core.shutdown()


def test_submit_exception_marks_error_not_zombie():
    # A DB write inside submit() (outside run_conversation's error handling) that
    # raises must mark the sub-session 'error', never leave it stuck 'running'.
    class BoomDB(SessionDB):
        # A persistência do turno é o lote atômico (save_messages) desde o
        # fix do achado 1 do review SUP-05; a bomba acompanha o ponto real.
        def save_messages(self, *args, **kwargs):
            raise RuntimeError("db down")

    boom = BoomDB(":memory:")
    core = OrchestrationCore(boom, _child_factory([[_text_response("hi")]]))
    try:
        sub_id = core.spawn("task")
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "error"
        assert "db down" in result["output"]
    finally:
        core.shutdown()
        boom.close()


def test_unknown_sub_id_errors(db):
    core = _core(db, [])
    try:
        assert "error" in core.steer("nope", "x")
        assert "error" in core.collect("nope")
        assert "error" in core.cancel("nope")
    finally:
        core.shutdown()


def test_cancel_running_interrupts(db):
    gate = threading.Event()
    started = threading.Event()

    class GatedClient(FakeClient):
        def create(self, **kwargs):
            started.set()
            gate.wait(5)
            return super().create(**kwargs)

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=GatedClient([_text_response("never returned in time")]),
        )

    core = OrchestrationCore(db, factory)
    try:
        sub_id = core.spawn("long task")
        assert started.wait(5)
        out = core.cancel(sub_id)
        assert out["ok"] is True
        assert out["cancelled"] == "running"
    finally:
        gate.set()
        core.shutdown()



# --- terminal transition for a leaf that never ran (issue #8) ---


def _gated_core(db, gate, started, *, max_concurrent=1):
    """A core whose every child blocks in the provider call until ``gate``."""

    class GatedClient(FakeClient):
        def create(self, **kwargs):
            started.set()
            gate.wait(5)
            return super().create(**kwargs)

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=GatedClient([_text_response("late")]),
        )

    return OrchestrationCore(db, factory, max_concurrent=max_concurrent)


def test_cancel_of_a_queued_sub_session_fires_on_done(db):
    """A leaf cancelled BEFORE the pool ever started it must still reach its
    completion hook. ``_fire_done`` used to run only from ``_run``, so a future
    that ``future.cancel()`` won never fired — and every consumer chained off
    on_done (the pipeline scheduler) waited out its whole barrier for a turn
    that was never going to happen."""
    gate, started = threading.Event(), threading.Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    fired: list[str] = []
    try:
        core.spawn("occupies the only worker")
        assert started.wait(5)
        queued = core.spawn("never starts", on_done=fired.append)
        out = core.cancel(queued)
        assert out["cancelled"] == "queued"  # the branch under test
        assert fired == [queued]
    finally:
        gate.set()
        core.shutdown()


def test_queued_cancel_reports_a_status_distinct_from_interrupted(db):
    """"Never ran" and "stopped mid-turn" are different outcomes: the first
    consumed nothing and is the one a budget can honestly refund."""
    gate, started = threading.Event(), threading.Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    try:
        running = core.spawn("occupies the only worker")
        assert started.wait(5)
        queued = core.spawn("never starts")
        core.cancel(queued)
        core.cancel(running)
        assert core.collect(queued)["status"] == "cancelled"
        assert core.collect(running)["status"] != "cancelled"
    finally:
        gate.set()
        core.shutdown()


def test_on_done_fires_at_most_once_across_cancel_and_shutdown(db):
    """Three threads can now reach ``_fire_done`` for one sub-session (its own
    worker, ``cancel``, ``shutdown``). The hook is a contract: exactly once."""
    gate, started = threading.Event(), threading.Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    fired: list[str] = []
    try:
        core.spawn("occupies the only worker")
        assert started.wait(5)
        queued = core.spawn("never starts", on_done=fired.append)
        core.cancel(queued)
        core.cancel(queued)
        core.shutdown(wait=False)
        assert fired == [queued]
    finally:
        gate.set()
        core.shutdown()


def test_shutdown_without_wait_settles_the_sub_sessions_it_drops(db):
    """``shutdown(wait=False)`` drops queued work at the pool level, bypassing
    ``cancel()`` entirely — so fixing ``cancel`` alone still strands every leaf
    the CANCEL path (service.cancel -> core.shutdown(wait=False)) throws away."""
    gate, started = threading.Event(), threading.Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    fired: list[str] = []
    try:
        core.spawn("occupies the only worker")
        assert started.wait(5)
        queued = [core.spawn(f"q{i}", on_done=fired.append) for i in range(3)]
        core.shutdown(wait=False)
        assert sorted(fired) == sorted(queued)
        assert all(core.collect(q)["status"] == "cancelled" for q in queued)
    finally:
        gate.set()
        core.shutdown()


# --- token split per sub-session (Fatia C) ---


def _usage_response(text, usage, stop_reason="end_turn"):
    return {"content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "usage": usage}


def test_collect_returns_the_cache_and_reasoning_split(db):
    """O que o transport ja sabia (cache/reasoning) deixa de morrer no _finalize."""
    core = _core(
        db,
        [[_usage_response("ok", {
            "input_tokens": 100, "output_tokens": 20,
            "cache_read_input_tokens": 60, "cache_creation_input_tokens": 40,
        })]],
    )
    try:
        sub_id = core.spawn("t")
        r = core.collect(sub_id, wait=True, timeout=5)
        assert r["tokens_in"] == 100 and r["tokens_out"] == 20
        assert r["cache_read_tokens"] == 60
        assert r["cache_write_tokens"] == 40
    finally:
        core.shutdown()


def test_collect_sums_every_api_call_of_the_turn(db):
    """Um turno com N chamadas custa as N — nao so a ultima (usage_total)."""
    core = _core(
        db,
        [[
            _usage_response("...", {"input_tokens": 100, "output_tokens": 10}, "pause_turn"),
            _usage_response("fim", {"input_tokens": 150, "output_tokens": 20}),
        ]],
    )
    try:
        sub_id = core.spawn("t")
        r = core.collect(sub_id, wait=True, timeout=5)
        assert r["tokens_in"] == 250 and r["tokens_out"] == 30
    finally:
        core.shutdown()


def test_collect_reports_the_leaf_provider_and_model(db):
    """Custo por AGENTE: sem (provider, model) nenhum leaf tem preco."""
    core = _core(db, [[_text_response("ok")]])
    try:
        sub_id = core.spawn("t")
        r = core.collect(sub_id, wait=True, timeout=5)
        assert r["provider"] == "anthropic"
        assert r["model"] == "claude-opus-4-8"
    finally:
        core.shutdown()


def test_a_sub_session_that_changed_model_mid_life_withholds_the_attribution(db):
    """Os tokens ACUMULAM entre turnos; o (provider, model) era REATRIBUIDO.

    Uma sub-sessao retomada (``delegate_task`` com ``resume_id``) sob outro
    modelo somava os tokens dos dois turnos e os atribuia inteiros ao ULTIMO —
    dinheiro do modelo errado. Mesma regra do ``NodeCost.merge``: discordou,
    retem a atribuicao (tokens continuam reportados, o preco nao)."""
    from types import SimpleNamespace

    from lohra.orchestration.core import OrchestrationCore, _SubSession

    agent = SimpleNamespace(model="claude-opus-4-8", provider=SimpleNamespace(name="anthropic"))
    sub = _SubSession(sub_id="s", session=SimpleNamespace(agent=agent), parent_id=None)

    OrchestrationCore._finalize(sub, {"final_response": "a"})
    assert (sub.provider, sub.model) == ("anthropic", "claude-opus-4-8")

    agent.model = "gpt-5.6-sol"  # a second turn on a different model
    agent.provider = SimpleNamespace(name="openai")
    OrchestrationCore._finalize(sub, {"final_response": "b"})
    assert sub.provider is None and sub.model is None

    # ...and it STAYS withheld. A third turn must not re-claim a total that
    # already spans two models just because the slot happens to read None —
    # "never attributed" and "attribution dropped" look identical otherwise.
    OrchestrationCore._finalize(sub, {"final_response": "c"})
    assert sub.provider is None and sub.model is None


def test_shutdown_settles_a_sub_session_spawned_during_teardown(db):
    """The snapshot ``shutdown(wait=False)`` takes is not the last word.

    It reads ``_children`` under the lock, RELEASES it, and only then closes the
    pool — so a ``spawn`` landing in that window is submitted to a pool about to
    drop it (``cancel_futures``) while being absent from the snapshot that
    settles the drops. Nobody fires its hook and nobody marks it terminal: the
    exact hang the settle path exists to prevent, one race later."""
    gate, started = threading.Event(), threading.Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    fired: list[str] = []
    latecomer: list[str] = []
    real_shutdown = core._pool.shutdown

    def racing_shutdown(*args, **kwargs):
        core._pool.shutdown = real_shutdown  # race once, then behave
        # Squeezes into the window: after the snapshot, before the pool closes.
        latecomer.append(core.spawn("slips through the crack", on_done=fired.append))
        return real_shutdown(*args, **kwargs)

    core._pool.shutdown = racing_shutdown
    try:
        core.spawn("occupies the only worker")
        assert started.wait(5)
        core.shutdown(wait=False)
        assert latecomer, "the racing spawn never ran"
        assert fired == latecomer  # the hook is a contract, window or no window
        assert core.collect(latecomer[0])["status"] == "cancelled"
    finally:
        gate.set()
        core.shutdown()


def test_steer_active_rejects_terminal_without_new_turn(db):
    core = _core(db, [[_text_response("first"), _text_response("must not run")]])
    try:
        sid = core.spawn("tarefa")
        out1 = core.collect(sid, wait=True, timeout=5)
        assert out1["output"] == "first"

        # a terminal sub-session: steer_active refuses without starting a turn
        res = core.steer_active(sid, "late")
        assert "not active" in res["error"]

        # proof no new turn ran: the 2nd scripted response ('must not run')
        # must never have been consumed
        out2 = core.collect(sid, wait=True, timeout=5)
        assert out2["output"] == "first"
    finally:
        core.shutdown()


def _active_steer_core(db, gate, started, *, followup):
    """A core whose child does a tool call, then blocks mid-turn on its second
    provider call until ``gate`` is released; ``followup`` is the scripted
    response of the potential follow-up turn (third provider call)."""

    class GatedClient(FakeClient):
        def create(self, **kwargs):
            if self.calls:  # second call onward — block until released
                started.set()
                gate.wait(5)
            return super().create(**kwargs)

    def factory():
        client = GatedClient(
            [
                _tool_call_response([("c1", "noop", {})]),
                _text_response("finished"),
                _text_response(followup),
            ]
        )
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
            tool_dispatch=lambda name, args: "{}",
        )

    return OrchestrationCore(db, factory)


def test_steer_active_busy_is_read_after_current_provider_call(db):
    # steer_active on a busy child: the steer lands in the inbox and is only
    # read after the in-flight provider call returns (same mechanics as the
    # ordinary steer, exercised through steer_active).
    gate, started = threading.Event(), threading.Event()
    settled: list[str] = []
    core = _active_steer_core(db, gate, started, followup="handled active steer")
    try:
        sub_id = core.spawn("start")
        assert started.wait(5)  # busy: blocked inside the 2nd provider call
        res = core.steer_active(sub_id, "late instruction", on_settle=settled.append)
        assert res == {"ok": True, "queued": True}
        gate.set()  # release the in-flight call
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["output"] == "handled active steer"
        assert settled == ["read"]  # settled exactly once, as read
    finally:
        gate.set()
        core.shutdown()


def test_steer_active_after_cancel_refuses(db):
    # A cancelled child is no longer active: steer_active refuses and never
    # starts a new turn.
    gate, started = threading.Event(), threading.Event()
    core = _active_steer_core(db, gate, started, followup="must not resurrect")
    try:
        sub_id = core.spawn("start")
        assert started.wait(5)
        out = core.cancel(sub_id)
        assert out["cancelled"] == "running"
        res = core.steer_active(sub_id, "late instruction")
        assert "cancelled" in res["error"]
        gate.set()
        core.collect(sub_id, wait=True, timeout=5)  # drain the interrupted turn
    finally:
        gate.set()
        core.shutdown()


def test_steer_active_accepted_then_cancel_no_followup(db):
    # steer_active accepted while busy, then cancel: the queued steer is
    # ABANDONED -- the turn ends interrupted, but the 3rd response never
    # becomes the output (no follow-up turn resurrects the cancelled child).
    gate, started = threading.Event(), threading.Event()
    settled: list[str] = []
    core = _active_steer_core(db, gate, started, followup="must not resurrect")
    try:
        sub_id = core.spawn("start")
        assert started.wait(5)
        res = core.steer_active(sub_id, "late instruction", on_settle=settled.append)
        assert res == {"ok": True, "queued": True}
        out = core.cancel(sub_id)
        assert out["cancelled"] == "running"
        gate.set()
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result.get("output") != "must not resurrect"
        assert settled == ["discarded"]  # settled exactly once, as discarded
    finally:
        gate.set()
        core.shutdown()


# ---------------------------------------------------------------------------
# SUP-03 settle-gap tests: the on_settle callback contract through the core's
# own lifecycle paths (queued child, mid-flight turn error).
# ---------------------------------------------------------------------------


def test_steer_active_accepts_queued_child_then_cancel_discards_once(db):
    # A queued child (max_concurrent=1, the only worker blocked in its turn) is
    # still live for steer_active: its upcoming turn will drain the inbox.
    # Cancelling it before the pool ever starts the turn must settle the
    # accepted steer exactly once as 'discarded' and mark it 'cancelled'.
    gate, started = threading.Event(), threading.Event()
    settled: list[str] = []
    core = _gated_core(db, gate, started, max_concurrent=1)
    try:
        core.spawn("occupies the only worker")
        assert started.wait(5)
        queued = core.spawn("never starts")
        res = core.steer_active(queued, "late instruction", on_settle=settled.append)
        assert res == {"ok": True, "queued": True}  # accepted while still queued
        out = core.cancel(queued)
        assert out["cancelled"] == "queued"  # the turn never started
        result = core.collect(queued, wait=True, timeout=5)
        assert result["status"] == "cancelled"
        assert settled == ["discarded"]  # fired exactly once, as discarded
    finally:
        gate.set()
        core.shutdown()


def test_run_turn_error_discards_accepted_steer_and_refuses_later_ones(db):
    # _run's except path: submit() died mid-flight, so the inbox can never be
    # drained again -- an already-accepted steer settles 'discarded' exactly
    # once, and the sub-session stops accepting steer_active. Drives _run
    # directly with an instance-level stubbed submit: deterministic (no
    # threads, no gates) while the inbox/settle semantics stay the REAL
    # GatewaySession ones.
    from lohra.gateway.session import GatewaySession
    from lohra.orchestration.core import _SubSession

    core = _core(db, [])
    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=FakeClient([_text_response("never reached")]),
    )
    session = GatewaySession("sub_boom", agent, db, on_compaction=None)
    settled: list[str] = []
    session.enqueue_steer("accepted before the boom", on_settle=settled.append)

    def boom(text, emit):
        raise RuntimeError("turn exploded")

    session.submit = boom  # instance attribute: scoped to this test only

    sub = _SubSession(sub_id="sub_boom", session=session, parent_id=None)
    with core._lock:
        core._children["sub_boom"] = sub
    try:
        core._run("sub_boom", "start")
        result = core.collect("sub_boom")
        assert result["status"] == "error"
        assert "turn exploded" in result["output"]
        assert settled == ["discarded"]  # fired exactly once, as discarded
        assert session.drain_steers() == []  # inbox emptied; nothing resurrects
        # dead for steer_active: no turn will ever drain this inbox again
        res = core.steer_active("sub_boom", "late")
        assert "not active" in res["error"]
    finally:
        core.shutdown()


# ---------------------------------------------------------------------------
# SUP-03: synchronous long tool non-interruption. A steer accepted while the
# turn is blocked INSIDE a synchronous tool must neither preempt the tool nor
# start a turn of its own: it stays in the inbox until the NEXT loop iteration
# reads it at the tail of the history (after the tool result), and settles
# exactly once, as 'read', only when actually delivered.
# ---------------------------------------------------------------------------


def test_steer_active_during_synchronous_long_tool_does_not_preempt(db):
    """Steer aceito com a tool síncrona BLOQUEADA não interrompe o turno.

    Determinístico (gates, sem sleeps): a tool seta ``tool_started`` e espera
    ``tool_gate``; enquanto ela não retorna, o steer é aceito no inbox mas
    nada acontece (sem settle, sem nova provider call). Ao liberar a tool, a
    iteração seguinte lê o steer no TAIL — depois do tool_result da MESMA
    conversa — e o settle dispara exatamente uma vez, como 'read'."""
    tool_gate = threading.Event()
    tool_started = threading.Event()
    tool_returned: list[str] = []
    settled: list[str] = []
    clients: list[FakeClient] = []

    def gated_dispatch(name, args):
        tool_started.set()
        tool_gate.wait(5)
        tool_returned.append(name)
        return '{"notified": "ok"}'

    def factory():
        client = FakeClient(
            [
                _tool_call_response([("c1", "noop", {})]),
                _text_response("after tool"),
            ]
        )
        clients.append(client)
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
            tool_dispatch=gated_dispatch,
        )

    core = OrchestrationCore(db, factory)
    try:
        sub_id = core.spawn("start")
        assert tool_started.wait(5)  # o turno está travado DENTRO da tool

        steer_text = "meanwhile, remember sup-03"
        res = core.steer_active(sub_id, steer_text, on_settle=settled.append)
        assert res == {"ok": True, "queued": True}  # aceito no inbox

        # Nada aconteceu com o turno bloqueado: a tool não retornou, o steer
        # não foi lido (nada settled) e NENHUMA nova provider call — sem
        # preempção, sem turno próprio para o steer.
        assert tool_returned == []
        assert settled == []
        assert len(clients[0].calls) == 1

        tool_gate.set()  # a tool retorna; o turno segue para a iteração 2
        result = core.collect(sub_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["output"] == "after tool"
        assert tool_returned == ["noop"]  # a tool terminou normalmente

        # settle exatamente uma vez, como 'read' — só quando foi lido
        assert settled == ["read"]
        # exatamente duas provider calls: o steer nunca virou turno próprio
        assert len(clients[0].calls) == 2

        # A 2ª call contém o system-reminder com o steer, DEPOIS do
        # tool_result de c1 (tail da mesma conversa — a tool não foi
        # preemptada) e fora do system prompt congelado (Invariante #1).
        second = clients[0].calls[1]
        messages = second["messages"]
        reminders = [
            m
            for m in messages
            if isinstance(m.get("content"), str) and "<system-reminder>" in m["content"]
        ]
        assert len(reminders) == 1
        assert "sup-03" in reminders[0]["content"]
        tool_result_at = [
            i
            for i, m in enumerate(messages)
            if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and m["content"][0].get("type") == "tool_result"
            and m["content"][0].get("tool_use_id") == "c1"
        ]
        assert len(tool_result_at) == 1
        assert tool_result_at[0] < messages.index(reminders[0])
        assert "<system-reminder>" not in (second.get("system") or "")
    finally:
        tool_gate.set()
        core.shutdown()


# --- issue #60: a steered sub-session goes back to RUNNING --------------------
#
# ``TERMINAL_STATUSES`` is what every consumer of ``collect`` reads to decide
# "is this number a total?" (accounting) and "is this one idle?" (eviction).
# A sub-session steered into a second turn used to keep the status its first
# turn left behind, so both questions were answered wrong about a leaf that was
# provably running.


class _BlockingClient(FakeClient):
    """Blocks inside the provider call from the ``block_from``-th call onward."""

    def __init__(self, responses, gate, started, block_from=1):
        super().__init__(responses)
        self._gate = gate
        self._started = started
        self._block_from = block_from

    def create(self, **kwargs):
        if len(self.calls) >= self._block_from:
            self._started.set()
            self._gate.wait(5)
        return super().create(**kwargs)


def _agent_with(client):
    return Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
    )


def _steerable_core(db, gate, started, **kwargs):
    """A core whose FIRST child answers turn 1 and then blocks in turn 2; every
    later child answers straight away (so a spawn at the cap is cheap)."""
    made: list[int] = []

    def factory():
        made.append(1)
        if len(made) == 1:
            return _agent_with(
                _BlockingClient(
                    [_text_response("first"), _text_response("second")], gate, started
                )
            )
        return _agent_with(FakeClient([_text_response("other")]))

    return OrchestrationCore(db, factory, **kwargs)


def test_steered_subsession_reads_running_again_during_its_second_turn(db):
    gate = threading.Event()
    started = threading.Event()
    core = _steerable_core(db, gate, started)
    try:
        sub_id = core.spawn("start")
        first = core.collect(sub_id, wait=True, timeout=5)
        assert first["status"] == "complete"
        assert first["tokens_in"] == 5  # the whole bill of turn 1

        assert core.steer(sub_id, "now the second thing") == {"ok": True, "queued": False}
        # The turn is committed the moment steer submits it — before the pool
        # worker even picks it up, the status must already say so.
        assert core.collect(sub_id)["status"] == "running"

        assert started.wait(5)  # turn 2 is now inside the provider call
        mid = core.collect(sub_id)
        assert mid["status"] == "running"
        assert mid["tokens_in"] == 5  # a number still moving, and it says so

        gate.set()
        final = core.collect(sub_id, wait=True, timeout=5)
        assert final["status"] == "complete"
        assert final["output"] == "second"
        assert final["tokens_in"] == 10  # both turns, now a total
    finally:
        gate.set()
        core.shutdown()


def test_a_sub_session_in_its_second_turn_is_never_evicted(db):
    gate = threading.Event()
    started = threading.Event()
    core = _steerable_core(db, gate, started, max_children=1)
    try:
        sub_id = core.spawn("start")
        core.collect(sub_id, wait=True, timeout=5)
        core.steer(sub_id, "again")
        assert started.wait(5)  # running its second turn, registry at cap (1)

        other = core.spawn("another")  # must not evict a sub-session in flight
        assert "error" not in core.collect(sub_id)
        assert "error" not in core.collect(other)

        gate.set()
        assert core.collect(sub_id, wait=True, timeout=5)["status"] == "complete"
    finally:
        gate.set()
        core.shutdown()


def test_eviction_spares_a_live_future_whatever_the_status_says(db):
    """The status reset is one guard; the live future is the other. Forcing a
    terminal status onto a sub-session whose turn is provably inside the
    provider call pins the second one on its own."""
    gate = threading.Event()
    started = threading.Event()
    core = _steerable_core(db, gate, started, max_children=1)
    try:
        sub_id = core.spawn("start")
        core.collect(sub_id, wait=True, timeout=5)
        core.steer(sub_id, "again")
        assert started.wait(5)
        with core._lock:  # white-box: pretend the reset never happened
            core._children[sub_id].status = "complete"

        core.spawn("another")
        assert "error" not in core.collect(sub_id)  # a running turn survives the cap
    finally:
        gate.set()
        core.shutdown()


def test_a_steered_turn_cancelled_before_it_runs_fires_a_freshly_armed_hook(db):
    """The completion hook is CONSUMED when it fires, and a steered turn re-arms
    the seam: a ``watch_done`` installed on the queued second turn still gets its
    terminal, even when the turn is cancelled before the pool starts it."""
    gate = threading.Event()
    started = threading.Event()
    made: list[int] = []

    def factory():
        made.append(1)
        if len(made) == 1:
            return _agent_with(
                FakeClient([_text_response("first"), _text_response("second")])
            )
        return _agent_with(
            _BlockingClient([_text_response("held")], gate, started, block_from=0)
        )

    core = OrchestrationCore(db, factory, max_concurrent=1)
    turn_one: list[str] = []
    turn_two: list[str] = []
    try:
        sub_id = core.spawn("start", on_done=turn_one.append)
        assert core.collect(sub_id, wait=True, timeout=5)["status"] == "complete"
        assert turn_one == [sub_id]  # fired once, and the slot is free again

        core.spawn("hold the only worker")
        assert started.wait(5)

        assert core.steer(sub_id, "again") == {"ok": True, "queued": False}
        assert core.collect(sub_id)["status"] == "running"  # queued IS running
        assert core.watch_done(sub_id, turn_two.append) is True

        core.cancel(sub_id)
        # "interrupted", not "cancelled": this sub-session HAS run a turn, and
        # "cancelled" is the status that refunds a lifetime slot downstream (#60).
        assert core.collect(sub_id)["status"] == "interrupted"
        assert turn_two == [sub_id]
        assert turn_one == [sub_id]  # turn 1's hook never fires twice
    finally:
        gate.set()
        core.shutdown()
