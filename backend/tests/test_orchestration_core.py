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
        def save_message(self, *args, **kwargs):
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
