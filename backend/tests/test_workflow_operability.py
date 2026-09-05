"""Operability (CC-parity M6) — a workflow run you can always look at.

Before this, a run in flight was a black box: ``workflow_status`` reported the
token budget off the LIVE engine but nothing about WHERE the run was, there was
no way to see the other runs at all, no way to stop one without throwing its
work away, and the only way to learn a run had finished was to poll it. Four
things close that:

- **progress**: the engine keeps a thread-safe map of per-node state
  (pending/running/complete/null, plus settled/total items for a ``pipeline``),
  snapshotted under its own lock and reported by ``status`` off the live engine —
  the same mid-run read the token budget already used;
- **``workflow_list``**: every run this service knows, newest first, capped;
- **``workflow_pause``**: a MANUAL pause reusing the run's ``PauseSignal`` with
  reason ``user_requested`` — stop scheduling, let the in-flight leaves finish
  and be charged, keep the finished cells, arm no auto-resume, teach ``library``
  nothing, and resume later without having to raise any budget;
- **notification**: an ``on_run_done`` callback the equip wiring points at the
  owning session's steer INBOX — never at the frozen system prompt (Invariante
  #1); a run nobody owns is a silent no-op.

Every leaf here costs a deterministic 8 tokens (fake usage 5 in / 3 out). No
real sleeps: a gate Event tells the test the provider call is really in flight,
and the test releases it.
"""

import json
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.workflow.engine import USER_PAUSE
from lohra.workflow.service import MAX_LISTED_RUNS, RunState, WorkflowService
from lohra.workflow.tools import WorkflowTool, register_workflow_tool_schemas
from tests.test_workflow_pipeline import ScriptedClient
from tests.test_workflow_quota import TimerFactory

LEAF_COST = 8  # one fake turn: 5 input + 3 output tokens


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}
_ONE_NODE_SCHEMA = {
    "meta": {"name": "nulls", "version": 1},
    "nodes": [
        {
            "id": "a",
            "type": "agent",
            "prompt": "go",
            "schema": {
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "string"}},
            },
        }
    ],
}
_PIPELINE = {
    "meta": {"name": "pipe", "version": 1},
    "nodes": [
        {
            "id": "p",
            "type": "pipeline",
            "items": "${args.items}",
            "stages": [{"prompt": "do ${item}"}],
        }
    ],
}


def _ok(_prompt: str) -> str:
    return "R"


def _gate():
    """(entered, release, responder) — the responder announces that it is really
    inside the provider call, then blocks until the test lets it go. This is what
    makes "while the run is still going" deterministic without a sleep."""
    entered, release = threading.Event(), threading.Event()

    def responder(_prompt: str) -> str:
        entered.set()
        release.wait(5)
        return "R"

    return entered, release, responder


def _service(db, home, responder, *, timers=None, on_run_done=None):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=home, on_run_done=on_run_done)
    if timers is not None:
        svc.set_autoresume(
            AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: 1000.0)
        )
    return svc


# --- 1. progress: the tracker itself ------------------------------------


def test_a_fresh_tracker_reports_every_node_pending():
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["a", "b"])
    assert tracker.snapshot() == {
        "total": 2,
        "done": 0,
        "running": 0,
        "pending": 2,
        "nodes": [{"id": "a", "state": "pending"}, {"id": "b", "state": "pending"}],
    }


def test_the_tracker_counts_a_nulled_node_as_done():
    """A node that nulled is FINISHED, not stuck — counting it as pending would
    leave a terminal run reporting work that will never happen. The per-node
    state still says ``null``, so "done" never means "went well"."""
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["a", "b"])
    tracker.mark_running("a")
    tracker.settle("a", None)
    tracker.mark_running("b")
    snap = tracker.snapshot()
    assert (snap["done"], snap["running"], snap["pending"]) == (1, 1, 0)
    assert snap["nodes"][0] == {"id": "a", "state": "null"}


def test_the_tracker_carries_per_item_progress_for_a_fan_out():
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["p"])
    tracker.mark_running("p")
    tracker.note_items("p", 2, 5)
    assert tracker.snapshot()["nodes"][0] == {
        "id": "p",
        "state": "running",
        "items": {"done": 2, "total": 5},
    }


def test_item_progress_never_walks_backwards():
    """The pipeline's workers publish AFTER releasing their own lock, so two of
    them can land out of order. Settled counts only ever grow, so the later
    (smaller) report is stale — taking it would leave a finished fan-out
    permanently reporting fewer items than it settled."""
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["p"])
    tracker.note_items("p", 2, 5)
    tracker.note_items("p", 1, 5)  # a straggler from the worker that ran first
    assert tracker.snapshot()["nodes"][0]["items"] == {"done": 2, "total": 5}


def test_the_snapshot_is_a_copy_nobody_can_mutate_from_outside():
    """The snapshot crosses a thread boundary into the agent's reply. Handing out
    the live dict would let a reader corrupt the run's own bookkeeping."""
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["a"])
    snap = tracker.snapshot()
    snap["nodes"][0]["state"] = "tampered"
    snap["nodes"].append({"id": "ghost"})
    assert tracker.snapshot()["nodes"] == [{"id": "a", "state": "pending"}]


def test_an_unknown_node_is_ignored_rather_than_invented():
    from lohra.workflow.progress import ProgressTracker

    tracker = ProgressTracker()
    tracker.reset(["a"])
    tracker.mark_running("ghost")
    tracker.settle("ghost", "x")
    tracker.note_items("ghost", 1, 2)
    assert tracker.snapshot()["total"] == 1


# --- 2. progress: read off the LIVE engine ------------------------------


def test_progress_is_visible_while_the_run_is_still_going(db, tmp_path):
    """The mid-run read the token budget already proved: there is no RunResult
    yet, and mid-run is exactly when knowing where the run is changes what the
    agent does."""
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5), "the first leaf never reached the provider"
        out = svc.status(run_id)  # no wait: the first leaf is still blocked
        assert out["status"] == "running"
        assert "nodes_total" not in out  # M5 contract: no RunResult fields early
        assert out["progress"] == {
            "total": 2,
            "done": 0,
            "running": 1,
            "pending": 1,
            "nodes": [{"id": "a", "state": "running"}, {"id": "b", "state": "pending"}],
        }
    finally:
        release.set()
        svc.shutdown()


def test_progress_reports_every_node_settled_once_the_run_is_done(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert out["progress"] == {
            "total": 2,
            "done": 2,
            "running": 0,
            "pending": 0,
            "nodes": [{"id": "a", "state": "complete"}, {"id": "b", "state": "complete"}],
        }
    finally:
        svc.shutdown()


def test_a_node_that_nulled_says_so_instead_of_reading_as_complete(db, tmp_path):
    svc = _service(db, tmp_path, _ok)  # "R" never satisfies the node's schema
    try:
        run_id = svc.start(_ONE_NODE_SCHEMA, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=20)
        assert out["status"] == "failed"
        assert out["progress"]["nodes"] == [{"id": "a", "state": "null"}]
        assert out["progress"]["done"] == 1  # finished — just not well
    finally:
        svc.shutdown()


def test_a_pipeline_node_reports_its_items(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_PIPELINE, {"items": ["x", "y", "z"]})["run_id"]
        out = svc.status(run_id, wait=True, timeout=20)
        assert out["status"] == "complete"
        assert out["progress"]["nodes"] == [
            {"id": "p", "state": "complete", "items": {"done": 3, "total": 3}}
        ]
    finally:
        svc.shutdown()


# --- 3. workflow_list ----------------------------------------------------


def test_list_runs_reports_each_run_newest_first(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        first = svc.start(_TWO_NODE, {}, token_budget=500)["run_id"]
        svc.status(first, wait=True, timeout=10)
        second = svc.start({**_TWO_NODE, "meta": {"name": "other", "version": 1}}, {})["run_id"]
        svc.status(second, wait=True, timeout=10)
        listed = svc.list_runs()
        assert [r["run_id"] for r in listed] == [second, first]
        assert listed[1] == {
            "run_id": first,
            "name": "demo",
            "status": "complete",
            "nodes_done": 2,
            "nodes_total": 2,
            "tokens_spent": 2 * LEAF_COST,
            "token_budget": 500,
            "overrun_max": 0,  # never over its ceiling — #81's field, unconditional
        }
        assert listed[0]["token_budget"] is None  # no ceiling asked for
    finally:
        svc.shutdown()


def test_list_runs_caps_what_it_returns(db, tmp_path):
    """The listing rides back inside a tool result the model reads — unbounded,
    a long-lived dashboard would eventually hand it hundreds of rows."""
    svc = _service(db, tmp_path, _ok)
    try:
        for index in range(MAX_LISTED_RUNS + 10):
            svc._runs[f"r{index}"] = RunState(
                run_id=f"r{index}", seq=index, name="x", status="complete"
            )
        listed = svc.list_runs()
        assert len(listed) == MAX_LISTED_RUNS
        assert listed[0]["run_id"] == f"r{MAX_LISTED_RUNS + 9}"
        # A run whose engine never existed still lists, with honest zeros.
        assert listed[0]["tokens_spent"] == 0 and listed[0]["token_budget"] is None
    finally:
        svc.shutdown()


# --- 4. workflow_pause: the manual, resumable stop ----------------------


def test_a_manual_pause_stops_scheduling_and_keeps_what_was_in_flight(db, tmp_path):
    entered, release, responder = _gate()
    timers = TimerFactory()
    svc = _service(db, tmp_path, responder, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        assert svc.pause(run_id) == {"ok": True, "run_id": run_id, "status": "pausing"}
        release.set()  # the leaf already in flight was paid for: let it land
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == USER_PAUSE == "user_requested"
        assert out["outputs"] == {"a": "R"}  # its answer was kept, not discarded
        assert out["tokens_in"] + out["tokens_out"] == LEAF_COST  # and charged
        assert out["progress"]["nodes"][1] == {"id": "b", "state": "pending"}
        assert timers.timers == []  # nothing will wake it on its own...
        assert out["resume_at"] is None
        assert "resume_run_id" in out["hint"]  # ...so the reply says what does
    finally:
        release.set()
        svc.shutdown()


def test_a_manual_pause_teaches_the_library_nothing(db, tmp_path):
    """Same veto as a cancel and a quota pause: the operator stopped this run,
    so neither certifying the shape nor blaming it is a lesson worth learning."""
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        svc.pause(run_id)
        release.set()
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert svc.list_templates() == []
        assert svc.recent_insights() == []
    finally:
        release.set()
        svc.shutdown()


def test_a_manually_paused_run_resumes_without_raising_any_budget(db, tmp_path):
    """No budget was involved, so there is nothing to raise — a resume that
    demanded one would make the manual pause a one-way door."""
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        svc.pause(run_id)
        release.set()
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert svc.resume(run_id)["status"] == "started"
        final = svc.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
        assert final["outputs"]["b"] == "R"  # the node that never ran, ran
    finally:
        release.set()
        svc.shutdown()


def test_resuming_a_manually_paused_run_still_refuses_a_spent_budget(db, tmp_path):
    """The manual pause does not excuse a blown ceiling: if the run ALSO spent
    its token_budget, raise-only still applies or the resume re-pauses instantly."""
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=LEAF_COST)["run_id"]
        assert entered.wait(5)
        svc.pause(run_id)
        release.set()
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused" and out["reason"] == USER_PAUSE
        refused = svc.start(_TWO_NODE, {}, resume_run_id=run_id)
        assert "already spent" in refused["error"]
        raised = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=LEAF_COST * 4)
        assert raised["status"] == "started"
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        release.set()
        svc.shutdown()


def test_pausing_what_is_not_running_says_which_run_and_why(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        assert "no workflow run" in svc.pause("nope")["error"]
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        error = svc.pause(run_id)["error"]
        assert run_id in error and "complete" in error
    finally:
        svc.shutdown()


# --- 5. notification: the run tells the session it finished -------------


def test_a_finished_run_notifies_with_its_status_and_its_spend(db, tmp_path):
    seen: list[tuple] = []
    svc = _service(db, tmp_path, _ok, on_run_done=lambda *args: seen.append(args))
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        assert len(seen) == 1
        _owner, notified_id, status, summary = seen[0]
        assert (notified_id, status) == (run_id, "complete")
        assert "demo" in summary and run_id[:8] in summary
        assert "complete" in summary and str(2 * LEAF_COST) in summary
    finally:
        svc.shutdown()


def test_a_paused_run_notifies_too(db, tmp_path):
    """"Paused" is exactly the status worth interrupting the agent for — it is
    the one that needs a decision."""
    seen: list[tuple] = []
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder, on_run_done=lambda *args: seen.append(args))
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        svc.pause(run_id)
        release.set()
        svc.status(run_id, wait=True, timeout=10)
        assert [s[2] for s in seen] == ["paused"]
    finally:
        release.set()
        svc.shutdown()


def test_a_cancelled_run_notifies_nobody(db, tmp_path):
    """The agent asked for the stop — telling it what it just did is noise in a
    turn it is already steering."""
    seen: list[tuple] = []
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder, on_run_done=lambda *args: seen.append(args))
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        svc.cancel(run_id)
        release.set()
        svc.status(run_id, wait=True, timeout=10)
        assert seen == []
    finally:
        release.set()
        svc.shutdown()


def test_a_raising_notifier_never_takes_the_run_down_with_it(db, tmp_path):
    def boom(*_args):
        raise RuntimeError("inbox exploded")

    svc = _service(db, tmp_path, _ok, on_run_done=boom)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete" and "error" not in out
    finally:
        svc.shutdown()


def test_the_equip_wiring_drops_the_line_in_the_owning_sessions_inbox(db, tmp_path):
    """The steer INBOX is the only legal channel: the system prompt is frozen for
    the session's whole life (Invariante #1), so a finished run reaches the agent
    as a system-reminder in the tail of the next iteration, never as a rewrite."""
    from lohra.agent.equip import bind_workflow_notifier

    class Inbox:
        def __init__(self) -> None:
            self.texts: list[str] = []

        def enqueue_steer(self, text: str) -> None:
            self.texts.append(text)

    inbox = Inbox()
    svc = _service(db, tmp_path, _ok)
    try:
        bind_workflow_notifier(svc, lambda sid: inbox if sid == "sess-1" else None)
        owned = svc.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        svc.status(owned, wait=True, timeout=10)
        assert len(inbox.texts) == 1 and "demo" in inbox.texts[0]
        # A run nobody owns is a silent no-op, never a crash.
        orphan = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(orphan, wait=True, timeout=10)
        assert len(inbox.texts) == 1
    finally:
        svc.shutdown()


def test_the_run_tool_stamps_the_session_that_launched_the_run():
    seen: dict = {}

    class Svc:
        def start(self, spec, args, **kwargs):
            seen.update(kwargs)
            return {"run_id": "r1", "status": "started"}

    WorkflowTool(Svc(), owner="sess-9").run({"spec": {"meta": {}}})
    assert seen["owner"] == "sess-9"


# --- 6. the tool surface the model actually sees ------------------------


def test_the_new_tools_are_registered_and_intercepted():
    from lohra.tools import registry

    register_workflow_tool_schemas()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"workflow_list", "workflow_pause"} <= names
    assert "error" in json.loads(registry.dispatch("workflow_list", {}))
    assert "error" in json.loads(registry.dispatch("workflow_pause", {"run_id": "x"}))


def test_the_new_tools_are_hidden_from_subagents_and_the_server():
    from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS, child_tool_definitions
    from lohra.tools import registry

    assert {"workflow_list", "workflow_pause"} <= _CHILD_EXCLUDED_TOOLS
    register_workflow_tool_schemas()
    # The server's allow-list builds on the very same filter (agentic.py), so
    # dropping them here drops them there too.
    exposed = {
        d["function"]["name"] for d in child_tool_definitions(tuple(registry.get_definitions()))
    }
    assert not ({"workflow_list", "workflow_pause"} & exposed)


def test_the_list_tool_returns_the_runs(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        out = json.loads(WorkflowTool(svc).list({}))
        assert [r["run_id"] for r in out["runs"]] == [run_id]
    finally:
        svc.shutdown()


def test_the_pause_tool_needs_a_run_id_and_forwards_it(db, tmp_path):
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        tool = WorkflowTool(svc)
        assert "error" in json.loads(tool.pause({}))
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        assert json.loads(tool.pause({"run_id": run_id}))["status"] == "pausing"
        release.set()
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        release.set()
        svc.shutdown()


def test_the_status_schema_documents_progress():
    from lohra.workflow.tools import _STATUS_SCHEMA

    assert "progress" in _STATUS_SCHEMA["description"]


def test_the_run_guidance_keeps_its_old_clauses_and_names_the_new_tools():
    from lohra.workflow.tools import RUN_GUIDANCE

    for pinned in ("token_budget", "resume_run_id", "paused", "workflow-authoring"):
        assert pinned in RUN_GUIDANCE
    assert "workflow_list" in RUN_GUIDANCE and "workflow_pause" in RUN_GUIDANCE


def test_the_skill_documents_the_operability_surface():
    from pathlib import Path

    from lohra.skills.store import SkillStore, builtin_root

    store = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),))
    skill = store.get("workflow-authoring")
    assert skill is not None
    for token in ("`workflow_list`", "`workflow_pause`", "`user_requested`", "`progress`"):
        assert token in skill.body, f"{token} undocumented in the skill"


def test_equip_binds_the_new_workflow_handlers(db, tmp_path):
    from lohra.agent.equip import build_session_dispatch, register_all_tools
    from lohra.memory.store import MemoryStore
    from lohra.skills.store import SkillStore

    register_all_tools()
    svc = _service(db, tmp_path, _ok)
    try:
        dispatch = build_session_dispatch(
            MemoryStore(tmp_path),
            SkillStore(tmp_path),
            db,
            workflow_service=svc,
            session_id="sess-7",
        )
        assert json.loads(dispatch("workflow_list", {}))["runs"] == []
        assert "error" in json.loads(dispatch("workflow_pause", {"run_id": "nope"}))
    finally:
        svc.shutdown()


def test_the_dashboard_session_owns_the_runs_it_launches(monkeypatch, tmp_path):
    """The notification is only real if the OWNER is real. The dashboard builds
    one agent per session through a factory; if that factory can't see the
    session id, every run it launches is owned by nobody and the inbox line of
    the test above never fires in production."""
    from lohra import cli

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # construction is offline
    seen: dict = {}

    def fake_start(self, spec, args, **kwargs):
        seen.update(kwargs)
        return {"run_id": "r1", "status": "started"}

    monkeypatch.setattr(WorkflowService, "start", fake_start)

    manager, app, _token = cli.build_dashboard_app(insecure=True)
    assert manager is not None and app is not None
    try:
        session = manager.create_session(session_id="sess-dash")
        out = session.agent.tool_dispatch("run_workflow", {"spec": {"meta": {}}})
        assert "error" not in json.loads(out)
        assert seen["owner"] == "sess-dash"
        # The revival path (a persisted session touched after a restart) too.
        seen.clear()
        manager._sessions.pop("sess-dash")
        revived = manager.get("sess-dash")
        revived.agent.tool_dispatch("run_workflow", {"spec": {"meta": {}}})
        assert seen["owner"] == "sess-dash"
    finally:
        app.state.cleanup()
