"""Live view of a workflow run — the operator stops being blind (WF-30).

A run used to be invisible while it mattered. ``workflow_status`` answered the
AGENT, on demand; the human who typed ``lohra chat`` saw nothing at all between
"started" and the final answer — and if they asked a FINISHED run about itself
from another process, the durable line reported ``0/0/0`` nodes, because progress
lived only in a ``ProgressTracker`` that died with the process.

Four things close that, and this file pins all four:

- **events**: the service fans one ``on_event(run_id, kind, payload)`` out for
  ``plan`` (the DAG, at acceptance), ``node`` (pending→running→settled), ``items``
  (a fan-out's count, rate-limited on an injected clock), ``fault`` (the text, the
  moment it happens — not at the end) and ``done``. A sink that raises is a broken
  sink, never a broken run;
- **the renderer** (``liveview``): pure, append-only text lines — no cursor games,
  so it reads the same in any terminal;
- **durable progress**: the snapshot is written to the run's own line at every
  node transition, so ``status``/``list`` from ANOTHER process report the nodes
  that really ran instead of honest-but-useless zeros;
- **``lohra workflow list|watch``**: the same durable line, read straight from
  SQLite with no provider and no LLM.

STDOUT DISCIPLINE (the load-bearing one): every live line goes to STDERR, always.
Under ``lohra chat --json`` stdout stays exactly one parseable object.

No real sleeps: clocks, timers and the watch loop's sleep are all injected.
"""

import json
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.events import (
    DONE,
    FAULT,
    ITEMS,
    NODE,
    PLAN,
    EventEmitter,
    plan_payload,
)
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

LEAF_COST = 8  # one fake turn: 5 input + 3 output tokens


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture
def file_db(tmp_path):
    """A FILE-backed store: these tests are about state that outlives the Python
    object holding it (the ``test_workflow_durable_state`` discipline)."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}
# A `verify` whose skeptics count is not a number raises inside the strategy:
# a deterministic engine fault, recorded and nulled, with a real agent node
# AFTER it so the test can see the fault arrive before the run is over.
# A tier the operator never mapped: the run works, but it warns — and THAT
# warning arriving only after the run was the operator's original complaint.
_TIERED = {
    "meta": {"name": "tiered", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go", "tier": "medium"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}
_FAULTY = {
    "meta": {"name": "boom", "version": 1},
    "nodes": [
        {"id": "bad", "type": "verify", "finding": "x", "skeptics": "not-a-number"},
        {"id": "a", "type": "agent", "prompt": "go"},
    ],
}


def _ok(_prompt: str) -> str:
    return "R"


def _gate():
    """(entered, release, responder) — the responder announces that it is really
    inside the provider call, then blocks until the test lets it go."""
    entered, release = threading.Event(), threading.Event()

    def responder(_prompt: str) -> str:
        entered.set()
        release.wait(5)
        return "R"

    return entered, release, responder


def _service(db, home, responder, *, on_event=None, clock=None):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    extra = {"clock": clock} if clock is not None else {}
    return WorkflowService(
        base_child_factory=factory, db=db, home=home, on_event=on_event, **extra
    )


def _sink():
    """(events, callback) — every event, in arrival order, under one lock."""
    events: list[tuple[str, str, dict]] = []
    lock = threading.Lock()

    def callback(run_id, kind, payload):
        with lock:
            events.append((run_id, kind, payload))

    return events, callback


def _kinds(events):
    return [kind for _run_id, kind, _payload in events]


# --- 1. the emitter: one sink, serialized, rate-limited, unbreakable -------


def test_the_emitter_forwards_every_kind_to_its_sink():
    events, sink = _sink()
    emitter = EventEmitter(sink)
    assert emitter.emit("r1", PLAN, {"nodes": []}) is True
    assert emitter.emit("r1", NODE, {"node_id": "a", "state": "running"}) is True
    assert _kinds(events) == [PLAN, NODE]
    assert events[1][0] == "r1" and events[1][2]["node_id"] == "a"


def test_the_sink_cannot_reach_back_into_the_payload_the_run_is_using():
    """The payload handed out is a copy: a sink that mutates what it got must
    not be able to corrupt the run's own bookkeeping (the ``snapshot`` rule)."""
    captured = []

    def sink(_run_id, _kind, payload):
        payload["node_id"] = "clobbered"
        captured.append(payload)

    payload = {"node_id": "a", "state": "running"}
    EventEmitter(sink).emit("r1", NODE, payload)
    assert payload["node_id"] == "a" and captured[0]["node_id"] == "clobbered"


def test_an_items_burst_is_rate_limited_to_one_per_interval():
    """A pipeline settles items from concurrent workers — a line per item would
    bury the terminal. One per second per node, on an INJECTED clock."""
    now = [100.0]
    events, sink = _sink()
    emitter = EventEmitter(sink, clock=lambda: now[0], items_interval=1.0)

    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 1, "total": 10}) is True
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 2, "total": 10}) is False
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 3, "total": 10}) is False
    now[0] += 1.5
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 4, "total": 10}) is True
    assert [payload["done"] for _r, _k, payload in events] == [1, 4]


def test_the_rate_limiter_is_per_node_not_per_run():
    now = [100.0]
    events, sink = _sink()
    emitter = EventEmitter(sink, clock=lambda: now[0], items_interval=1.0)
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 1, "total": 10}) is True
    assert emitter.emit("r1", ITEMS, {"node_id": "q", "done": 1, "total": 10}) is True
    assert len(events) == 2


def test_the_width_and_the_last_item_are_never_rate_limited_away():
    """``done == 0`` is the fan-out's WIDTH — news the moment it starts — and
    ``done == total`` is the finish. A limiter that ate either would leave a
    finished pipeline reporting 3/4 forever."""
    events, sink = _sink()
    emitter = EventEmitter(sink, clock=lambda: 100.0, items_interval=1.0)
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 0, "total": 4}) is True
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 1, "total": 4}) is False
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 4, "total": 4}) is True
    assert [payload["done"] for _r, _k, payload in events] == [0, 4]


def test_a_raising_sink_never_takes_the_run_down_with_it():
    def boom(_run_id, _kind, _payload):
        raise RuntimeError("the terminal exploded")

    emitter = EventEmitter(boom)
    assert emitter.emit("r1", NODE, {"node_id": "a"}) is True  # swallowed, reported


def test_an_emitter_with_no_sink_still_reports_what_passed_the_limiter():
    """Durable progress hangs off the same verdict, so it must not depend on
    anybody watching."""
    emitter = EventEmitter(None, clock=lambda: 100.0, items_interval=1.0)
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 1, "total": 9}) is True
    assert emitter.emit("r1", ITEMS, {"node_id": "p", "done": 2, "total": 9}) is False


def test_a_finished_run_stops_costing_the_limiter_memory():
    emitter = EventEmitter(None, clock=lambda: 100.0, items_interval=1.0)
    emitter.emit("r1", ITEMS, {"node_id": "p", "done": 1, "total": 9})
    emitter.emit("r1", DONE, {"status": "complete"})
    assert emitter.tracked_nodes() == 0


# --- 2. the plan: the DAG, before anything runs ----------------------------


def test_the_plan_payload_lists_the_dag_in_topological_order_with_its_deps():
    spec = validate_spec(
        {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "c", "type": "agent", "prompt": "${a} ${b}"},
                {"id": "a", "type": "agent", "prompt": "go", "tier": "medium"},
                {"id": "b", "type": "agent", "prompt": "go", "depends_on": ["a"]},
            ],
        }
    )
    payload = plan_payload("run-1", spec, name="x", token_budget=12000)
    assert payload["run_id"] == "run-1" and payload["token_budget"] == 12000
    assert [node["id"] for node in payload["nodes"]] == ["a", "b", "c"]
    assert payload["nodes"][0] == {"id": "a", "type": "agent", "depends_on": [], "tier": "medium"}
    assert payload["nodes"][1]["depends_on"] == ["a"]
    # ``c`` names neither in depends_on — the deps are the REFS it reads.
    assert payload["nodes"][2]["depends_on"] == ["a", "b"]


def test_the_plan_payload_carries_lint_warnings_when_given_any():
    spec = validate_spec({"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]})
    warnings = [{"rule": "disconnected_dag", "message": "2 nodes...", "node_id": None}]
    payload = plan_payload("run-1", spec, warnings=warnings)
    assert payload["warnings"] == warnings


def test_the_plan_payload_has_no_warnings_key_when_none_were_given():
    spec = validate_spec({"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]})
    assert "warnings" not in plan_payload("run-1", spec)
    assert "warnings" not in plan_payload("run-1", spec, warnings=[])


def test_the_plan_fires_before_the_run_id_comes_back(db, tmp_path):
    """The whole point: the DAG is on screen at launch, not at the end. The
    discriminator is that it is already there the instant ``start`` returns."""
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        out = svc.start(_TWO_NODE, {})
        assert _kinds(events)[0] == PLAN  # synchronously, before the pool sees it
        payload = events[0][2]
        assert payload["run_id"] == out["run_id"] and payload["name"] == "demo"
        assert [node["id"] for node in payload["nodes"]] == ["a", "b"]
        svc.status(out["run_id"], wait=True, timeout=10)
    finally:
        svc.shutdown()


def test_a_refused_spec_announces_no_plan(db, tmp_path):
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        assert "error" in svc.start({"meta": {}, "nodes": [{"id": "a", "type": "nope"}]}, {})
        assert events == []
    finally:
        svc.shutdown()


# --- 3. the run's own events ----------------------------------------------


def test_every_node_reports_running_then_settled_with_the_counters(db, tmp_path):
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        nodes = [payload for _r, kind, payload in events if kind == NODE]
        assert [(n["node_id"], n["state"]) for n in nodes] == [
            ("a", "running"), ("a", "complete"), ("b", "running"), ("b", "complete"),
        ]
        assert nodes[1]["done"] == 1 and nodes[1]["total"] == 2
        assert nodes[3]["done"] == 2 and nodes[3]["tokens"] >= LEAF_COST
    finally:
        svc.shutdown()


def test_a_fault_is_announced_the_moment_it_happens(db, tmp_path):
    """The complaint that started this: the tier warning showed up at the END,
    after the run everyone was waiting on. It has to land while the run is still
    going — i.e. BEFORE the nodes that come after it settle."""
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        run_id = svc.start(_FAULTY, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "degraded"
        kinds = _kinds(events)
        fault_at = kinds.index(FAULT)
        settled_at = [
            i for i, (_r, kind, payload) in enumerate(events)
            if kind == NODE and payload["node_id"] == "a" and payload["state"] == "complete"
        ][0]
        assert fault_at < settled_at < kinds.index(DONE)
        assert "engine fault" in events[fault_at][2]["text"]
    finally:
        svc.shutdown()


def test_the_tier_warning_lands_while_the_run_is_still_running(db, tmp_path):
    """The complaint, literally: an unmapped tier warns, and that warning used to
    surface with the rollup — after the wait everyone was sitting through. It has
    to be on screen before the node after it settles."""
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        run_id = svc.start(_TIERED, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        fault_at = _kinds(events).index(FAULT)
        settled_b = [
            i for i, (_r, kind, payload) in enumerate(events)
            if kind == NODE and payload["node_id"] == "b" and payload["state"] == "complete"
        ][0]
        assert "tier" in events[fault_at][2]["text"] and fault_at < settled_b


    finally:
        svc.shutdown()


def test_a_nulled_node_settles_as_null_not_complete(db, tmp_path):
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        run_id = svc.start(_FAULTY, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        states = {
            payload["node_id"]: payload["state"]
            for _r, kind, payload in events
            if kind == NODE and payload["state"] != "running"
        }
        assert states == {"bad": "null", "a": "complete"}
    finally:
        svc.shutdown()


def test_the_done_event_carries_the_final_status_and_what_it_cost(db, tmp_path):
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        done = [payload for _r, kind, payload in events if kind == DONE]
        assert len(done) == 1
        assert done[0]["status"] == "complete" and done[0]["name"] == "demo"
        assert done[0]["done"] == 2 and done[0]["total"] == 2
        assert done[0]["tokens"] >= 2 * LEAF_COST
    finally:
        svc.shutdown()


def test_a_pipeline_reports_its_width_the_moment_it_starts(db, tmp_path):
    events, sink = _sink()
    svc = _service(db, tmp_path, _ok, on_event=sink)
    spec = {
        "meta": {"name": "pipe"},
        "nodes": [
            {"id": "p", "type": "pipeline", "items": "${args.items}",
             "stages": [{"prompt": "do ${item}"}]}
        ],
    }
    try:
        run_id = svc.start(spec, {"items": ["x", "y", "z"]})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        items = [payload for _r, kind, payload in events if kind == ITEMS]
        assert items and items[0] == {"node_id": "p", "done": 0, "total": 3, "tokens": 0}
        assert items[-1]["done"] == 3  # the finish is never rate-limited away
    finally:
        svc.shutdown()


def test_a_raising_event_sink_never_takes_the_run_down(db, tmp_path):
    def boom(_run_id, _kind, _payload):
        raise RuntimeError("the terminal exploded")

    svc = _service(db, tmp_path, _ok, on_event=boom)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


# --- 4. durable progress: the 0/0/0 the owner saw -------------------------


def test_progress_survives_the_process_that_ran_it(file_db, tmp_path):
    """The bug, stated: a run that FINISHED, asked about from another process,
    reported ``0/0/0`` — the tracker lived in memory and died with its process.
    Two services over one file db is the honest simulation of a restart."""
    first = _service(file_db, tmp_path, _ok)
    try:
        run_id = first.start(_TWO_NODE, {})["run_id"]
        assert first.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        first.shutdown()

    second = _service(file_db, tmp_path, _ok)
    try:
        out = second.status(run_id)  # a run this process never launched
        assert out["status"] == "complete"
        assert out["progress"]["done"] == 2 and out["progress"]["total"] == 2
        assert [node["id"] for node in out["progress"]["nodes"]] == ["a", "b"]
        assert [node["state"] for node in out["progress"]["nodes"]] == ["complete", "complete"]
    finally:
        second.shutdown()


def test_the_listing_reports_real_node_counts_for_a_run_only_sqlite_knows(file_db, tmp_path):
    first = _service(file_db, tmp_path, _ok)
    try:
        run_id = first.start(_TWO_NODE, {})["run_id"]
        first.status(run_id, wait=True, timeout=10)
    finally:
        first.shutdown()

    second = _service(file_db, tmp_path, _ok)
    try:
        entry = [row for row in second.list_runs() if row["run_id"] == run_id][0]
        assert (entry["nodes_done"], entry["nodes_total"]) == (2, 2)
    finally:
        second.shutdown()


def test_progress_is_on_disk_while_the_run_is_still_going(file_db, tmp_path):
    """Not only at the end: ``lohra workflow watch`` in another terminal reads
    this line, so it has to move while the run moves."""
    from lohra.workflow.runstate_store import RunStateStore

    entered, release, responder = _gate()
    svc = _service(file_db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        row = RunStateStore(file_db).load(run_id)  # a reader that shares nothing
        assert row is not None and row.progress is not None
        assert row.progress["total"] == 2
        assert row.progress["nodes"][0] == {"id": "a", "state": "running"}
    finally:
        release.set()
        svc.status(run_id, wait=True, timeout=10)
        svc.shutdown()


def test_the_durable_line_round_trips_progress(file_db):
    from lohra.workflow.runstate_store import RunStateStore

    store = RunStateStore(file_db, holder="A", clock=lambda: 42.0)
    snapshot = {
        "total": 2, "done": 1, "running": 1, "pending": 0,
        "nodes": [{"id": "a", "state": "complete"}, {"id": "b", "state": "running"}],
    }
    store.save(run_id="r1", name="demo", status="running", progress=snapshot)
    assert store.load("r1").progress == snapshot


def test_cancelling_a_run_from_another_process_keeps_the_progress_it_had(file_db):
    from lohra.workflow.runstate_store import RunStateStore

    store = RunStateStore(file_db, holder="A", clock=lambda: 42.0)
    snapshot = {"total": 2, "done": 1, "running": 0, "pending": 1, "nodes": []}
    store.save(run_id="r1", name="demo", status="running", progress=snapshot)
    assert store.mark_cancelled("r1") == "cancelled"
    row = store.load("r1")
    assert row.status == "cancelled" and row.progress == snapshot


def test_a_run_that_never_reached_a_node_reports_no_progress_block(file_db):
    """Shape-identical to the live path, which reports None while there is
    nothing to say — never an empty ``{total: 0}`` the reader has to special-case."""
    from lohra.workflow.runstate_store import RunStateStore, durable_rollup

    store = RunStateStore(file_db, holder="A", clock=lambda: 42.0)
    store.save(run_id="r1", name="demo", status="running")
    out = durable_rollup(store.load("r1"), spent_total=0, stale=True)
    assert "progress" not in out


# --- 5. the renderer: append-only lines, any terminal ----------------------


def test_the_plan_renders_as_a_numbered_dag():
    from lohra.workflow.liveview import render_event

    lines = render_event(
        "8ebe0496aaaa",
        PLAN,
        {
            "run_id": "8ebe0496aaaa", "name": "parecer-x", "token_budget": 12000,
            "nodes": [
                {"id": "analyze_a", "type": "agent", "depends_on": [], "tier": "medium"},
                {"id": "consolidar", "type": "agent", "depends_on": ["analyze_a"]},
            ],
        },
    )
    assert lines[0] == "workflow parecer-x (8ebe0496) · budget 12000 tok"
    assert lines[1] == "  1. analyze_a (agent, tier medium)"
    assert lines[2] == "  2. consolidar (agent) <- depends: analyze_a"


def test_the_plan_renders_a_lint_warning_line_after_the_dag():
    from lohra.workflow.liveview import render_event

    lines = render_event(
        "8ebe0496aaaa",
        PLAN,
        {
            "run_id": "8ebe0496aaaa", "name": "solo", "token_budget": None,
            "nodes": [{"id": "a", "type": "agent", "depends_on": []}],
            "warnings": [{"rule": "disconnected_dag", "message": "2 nodes share no edge"}],
        },
    )
    assert any("⚠" in line and "2 nodes share no edge" in line for line in lines)


def test_a_settled_node_renders_with_its_counters_and_spend():
    from lohra.workflow.liveview import render_event

    lines = render_event(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "analyze_a", "state": "complete",
         "done": 2, "total": 4, "running": 1, "pending": 1, "tokens": 8123},
    )
    assert lines == ["[8ebe0496] analyze_a ✓ · 2/4 nodes · 8.1k tok"]


def test_a_fault_renders_as_a_warning_line():
    from lohra.workflow.liveview import render_event

    lines = render_event("8ebe0496aaaa", FAULT, {"text": "bad: engine fault"})
    assert lines == ["[8ebe0496] ⚠ bad: engine fault"]


def test_a_run_id_travels_with_every_rendered_line():
    """Two runs can be in flight at once — a line that does not name its run is
    unreadable the moment there are two."""
    from lohra.workflow.liveview import render_event

    lines = render_event(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "running", "done": 0, "total": 2, "tokens": 0},
    )
    assert lines == ["[8ebe0496] a ▸ · 0/2 nodes · 0 tok"]


def test_a_fan_out_renders_its_settled_count():
    from lohra.workflow.liveview import render_event

    lines = render_event("8ebe0496aaaa", ITEMS, {"node_id": "p", "done": 3, "total": 8})
    assert lines == ["[8ebe0496] p · items 3/8"]


def test_the_done_line_says_how_it_ended():
    from lohra.workflow.liveview import render_event

    lines = render_event(
        "8ebe0496aaaa",
        DONE,
        {"name": "parecer-x", "status": "degraded", "done": 4, "total": 4, "tokens": 12300},
    )
    assert lines == ["[8ebe0496] workflow parecer-x finished: degraded · 4/4 nodes · 12.3k tok"]


def test_an_unknown_kind_renders_nothing_rather_than_crashing_the_turn():
    from lohra.workflow.liveview import render_event

    assert render_event("x", "something-new", {"whatever": 1}) == []


def test_a_terminal_that_cannot_encode_the_symbols_still_gets_the_line():
    """stderr on a C-locale terminal raises on ``✓``. A progress line must never
    be the thing that kills a turn."""
    import io

    from lohra.workflow.liveview import write_lines

    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    write_lines(["[abc] a ✓ · 1/2 nodes"], stream)
    stream.flush()
    written = stream.buffer.getvalue().decode("ascii")
    assert "[abc] a" in written and "1/2 nodes" in written


# --- 6. `lohra workflow list|watch` — the durable line, no LLM -------------


def _seed(home, run_id, *, status="complete", name="demo", progress=None, now=1000.0,
          live=False):
    """Write a run's line straight to SQLite — no service, no provider.

    ``live`` also takes the run's lease, which is what tells a ``running`` row
    that still has an owner from one whose process died (``stale``)."""
    from lohra.memory.paths import state_db_path
    from lohra.workflow.runstate_store import RunStateStore

    database = SessionDB(str(state_db_path()))
    try:
        store = RunStateStore(database, holder="seed", clock=lambda: now)
        # Explicitly UNFENCED, like every other administrative write (issue #12):
        # this store owns nothing, so its default fence is "I cannot present one"
        # — which a seed, unlike a straggling run thread, is entitled to bypass.
        store.save(
            run_id=run_id, name=name, status=status, progress=progress,
            token_budget=500, fence=None,
        )
        if live:
            RunStateStore(database, holder="owner").acquire(run_id)
    finally:
        database.close()


_PROGRESS = {
    "total": 3, "done": 2, "running": 1, "pending": 0,
    "nodes": [
        {"id": "a", "state": "complete"},
        {"id": "b", "state": "null"},
        {"id": "c", "state": "running"},
    ],
}


def test_workflow_list_prints_the_durable_rows(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", name="parecer", progress=_PROGRESS)
    assert cli.run_workflow_cmd("list") == 0
    out = capsys.readouterr().out
    assert "abc123de" in out and "parecer" in out and "2/3" in out


def test_workflow_list_says_so_when_there_is_nothing(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_workflow_cmd("list") == 0
    assert "no workflow runs" in capsys.readouterr().out


def test_watch_prints_the_run_and_exits_when_it_is_terminal(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    slept = []
    assert cli.run_workflow_cmd(
        "watch", run_id="abc123def456", sleep=lambda s: slept.append(s)
    ) == 0
    assert "abc123de" in capsys.readouterr().out
    assert slept == []  # a finished run is never polled a second time


def test_watch_follows_a_run_until_it_stops(monkeypatch, tmp_path, capsys):
    """The poll loop is what makes ``watch`` useful — and the sleep is injected,
    so the test drives the clock instead of waiting on one."""
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="running", progress=_PROGRESS, live=True)

    def sleep(_seconds):
        _seed(tmp_path, "abc123def456", status="complete", progress={**_PROGRESS, "done": 3},
              now=1001.0)

    assert cli.run_workflow_cmd("watch", run_id="abc123def456", sleep=sleep) == 0
    out = capsys.readouterr().out
    assert "2/3" in out and "3/3" in out and "complete" in out


def test_watch_last_resolves_the_most_recent_run(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "older00000000", name="older", now=1000.0)
    _seed(tmp_path, "newer00000000", name="newer", now=2000.0)
    assert cli.run_workflow_cmd("watch", last=True, sleep=lambda s: None) == 0
    out = capsys.readouterr().out
    assert "newer" in out and "older" not in out


def test_watch_gives_up_on_a_run_whose_process_is_gone(monkeypatch, tmp_path, capsys):
    """A ``running`` row with nobody holding its lease never turns terminal — a
    poll loop that only watched for terminal statuses would spin forever."""
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="running", progress=_PROGRESS)
    slept = []
    assert cli.run_workflow_cmd(
        "watch", run_id="abc123def456", sleep=lambda s: slept.append(s)
    ) == 0
    err = capsys.readouterr().err
    assert "lost" in err or "stale" in err
    assert slept == []


def test_watch_without_a_target_errors(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_workflow_cmd("watch") == 2
    assert "run id" in capsys.readouterr().err


def test_watch_on_an_unknown_run_errors(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    assert cli.run_workflow_cmd("watch", run_id="nope", sleep=lambda s: None) == 1
    assert "no workflow run" in capsys.readouterr().err


# --- issue #24: watch/audit aceitam o prefixo curto que a própria list imprime ---


def test_watch_accepts_the_short_prefix_the_list_prints(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    # o fluxo natural: copiar os 8 chars da list e colar no watch
    assert cli.run_workflow_cmd("watch", run_id="abc123de", sleep=lambda s: None) == 0
    assert "abc123de" in capsys.readouterr().out


def test_watch_ambiguous_prefix_is_a_didactic_error(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    _seed(tmp_path, "abc123999999", status="complete", progress=_PROGRESS, now=1001.0)
    assert cli.run_workflow_cmd("watch", run_id="abc123", sleep=lambda s: None) == 2
    err = capsys.readouterr().err
    assert "ambiguous" in err
    assert "abc123def456" in err and "abc123999999" in err  # os candidatos, completos


def test_watch_prefix_with_like_wildcards_never_matches(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    # '%' é literal no prefixo, nunca curinga SQL — sem match, erro atual
    assert cli.run_workflow_cmd("watch", run_id="abc%", sleep=lambda s: None) == 1
    assert "no workflow run" in capsys.readouterr().err


def test_a_full_run_id_still_works_verbatim(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    assert cli.run_workflow_cmd("watch", run_id="abc123def456", sleep=lambda s: None) == 0


def test_audit_accepts_the_short_prefix_too(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _seed(tmp_path, "abc123def456", status="complete", progress=_PROGRESS)
    assert cli.run_workflow_cmd("audit", run_id="abc123de") == 0
    out = json.loads(capsys.readouterr().out)
    assert out.get("run_id") == "abc123def456"  # resolvido pro id completo


def test_the_workflow_subcommand_is_wired_into_the_parser():
    from lohra import cli

    args = cli.build_parser().parse_args(["workflow", "watch", "--last"])
    assert args.command == "workflow" and args.workflow_cmd == "watch" and args.last is True


# --- 7. stdout discipline: the live view is STDERR, always ----------------


_LEAF_MARK = "LEAF-PROMPT-MARK"
_CHAT_SPEC = {
    "meta": {"name": "chatty", "version": 1},
    "nodes": [{"id": "a", "type": "agent", "prompt": _LEAF_MARK}],
}


def _patch_workflow_client(monkeypatch):
    """A client that asks for ONE workflow run, then answers — and replies plainly
    to the leaf, so the run really executes."""
    from lohra import agent as agent_pkg

    class WorkflowFake(agent_pkg.ModelClient):
        def __init__(self):
            self.asked = False

        def _text(self, kwargs):
            return " ".join(
                message.get("content", "")
                for message in kwargs.get("messages") or []
                if isinstance(message.get("content"), str)
            )

        def create(self, **kwargs):
            if _LEAF_MARK in self._text(kwargs) or self.asked:
                return {
                    "content": [{"type": "text", "text": "pronto"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }
            self.asked = True
            return {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "run_workflow",
                     "input": {"spec": _CHAT_SPEC}}
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            }

        def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
            return self.create(**kwargs)

    fake = WorkflowFake()
    monkeypatch.setattr("lohra.agent.client.build_client", lambda profile, **kw: fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    return fake


def test_json_stdout_is_still_exactly_one_object_with_the_live_view_on(
    monkeypatch, tmp_path, capsys
):
    """THE contract. ``lohra chat --json`` is the orchestration surface: stdout is
    one parseable object and nothing else. The live view is for the human, so it
    goes to stderr — in BOTH modes, unconditionally."""
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _patch_workflow_client(monkeypatch)
    code = cli.run_chat("roda um workflow", provider="anthropic", json_output=True)
    captured = capsys.readouterr()
    assert code == 0
    envelope = json.loads(captured.out)  # stdout: ONLY the envelope
    assert envelope["input"] == "roda um workflow"
    # ...and the plan really was announced, on stderr, at launch.
    assert "workflow chatty (" in captured.err
    assert "1. a (agent)" in captured.err


def test_the_plan_reaches_stderr_in_plain_text_mode_too(monkeypatch, tmp_path, capsys):
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    _patch_workflow_client(monkeypatch)
    assert cli.run_chat("roda um workflow", provider="anthropic") == 0
    captured = capsys.readouterr()
    assert "workflow chatty (" in captured.err
    assert "workflow chatty (" not in captured.out  # never mixed into the answer


def test_items_lines_carry_the_tokens_already_landed():
    """Polish pedido pelo usuário ao vivo: quando `items 1/3` aparece, aquele item
    JÁ pousou e já foi cobrado — a linha deve mostrar o custo escalando, não 0→salto."""
    from lohra.workflow.liveview import render_event

    lines = render_event("abc12345", "items", {"node_id": "fan", "done": 1, "total": 3, "tokens": 4100})
    assert lines == ["[abc12345] fan · items 1/3 · 4.1k tok"]
    # sem tokens no payload (compat), a linha antiga continua valendo
    lines = render_event("abc12345", "items", {"node_id": "fan", "done": 0, "total": 3})
    assert lines == ["[abc12345] fan · items 0/3"]
