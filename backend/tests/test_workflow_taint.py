"""Tests for taint propagation → reduced leaf capability (Fase 8 §8.2 control 3)."""


import pytest

from lohra.agent.agent import Agent
from lohra.agent.taint import TaintTracker, is_tainting_tool, taint_wrap
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import tool_result
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool
from tests.test_loop import FakeClient, _text_response, _tool_call_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


# --- taint.py unit ---


def test_is_tainting_tool():
    assert is_tainting_tool("web_fetch")
    assert is_tainting_tool("web_search")
    assert is_tainting_tool("mcp_github_search")
    assert not is_tainting_tool("read_file")
    assert not is_tainting_tool("run_workflow")


def test_tracker_starts_clean_and_is_sticky():
    t = TaintTracker()
    assert t.tainted is False
    t.mark()
    assert t.tainted is True
    t.mark()  # idempotent
    assert t.tainted is True


def test_taint_wrap_marks_only_on_tainting_tool():
    tracker = TaintTracker()
    base = lambda name, args: f"ran:{name}"  # noqa: E731
    wrapped = taint_wrap(base, tracker)
    assert wrapped("read_file", {}) == "ran:read_file"  # passthrough
    assert tracker.tainted is False  # not tainting
    assert wrapped("web_fetch", {"url": "x"}) == "ran:web_fetch"
    assert tracker.tainted is True  # now tainted


# --- WorkflowTool feeds taint into start ---


class _CaptureService:
    def __init__(self):
        self.captured = {}

    def start(
        self,
        spec,
        args,
        *,
        resume_run_id=None,
        checkpoint_answers=None,
        token_budget=None,
        tainted=False,
        owner=None,
        agency_authored=False,
    ):
        self.captured["tainted"] = tainted
        self.captured["owner"] = owner
        return {"run_id": "r", "status": "started"}


_SPEC = {"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "p"}]}


def test_run_passes_taint_true_when_marked():
    svc = _CaptureService()
    tracker = TaintTracker()
    tracker.mark()
    WorkflowTool(svc, taint=tracker).run({"spec": _SPEC})
    assert svc.captured["tainted"] is True


def test_run_passes_taint_false_when_clean():
    svc = _CaptureService()
    WorkflowTool(svc, taint=TaintTracker()).run({"spec": _SPEC})
    assert svc.captured["tainted"] is False


def test_run_no_tracker_is_untainted():
    svc = _CaptureService()
    WorkflowTool(svc).run({"spec": _SPEC})
    assert svc.captured["tainted"] is False


# --- end-to-end through the real session dispatch ---


def test_taint_propagates_through_session_dispatch(tmp_path):
    from lohra.agent.equip import build_session_dispatch, register_all_tools
    from lohra.memory.store import MemoryStore
    from lohra.skills.store import SkillStore

    register_all_tools()
    svc = _CaptureService()
    dispatch = build_session_dispatch(
        MemoryStore(tmp_path), SkillStore(tmp_path), workflow_service=svc
    )
    # an mcp_* tool is tainting; unregistered → registry returns an error (no network),
    # but taint_wrap marks the tracker BEFORE dispatching.
    dispatch("mcp_fake_tool", {})
    dispatch("run_workflow", {"spec": _SPEC})
    assert svc.captured["tainted"] is True


def test_clean_session_dispatch_is_untainted(tmp_path):
    from lohra.agent.equip import build_session_dispatch, register_all_tools
    from lohra.memory.store import MemoryStore
    from lohra.skills.store import SkillStore

    register_all_tools()
    svc = _CaptureService()
    dispatch = build_session_dispatch(
        MemoryStore(tmp_path), SkillStore(tmp_path), workflow_service=svc
    )
    dispatch("run_workflow", {"spec": _SPEC})  # no tainting tool ran
    assert svc.captured["tainted"] is False


# --- end-to-end: taint actually denies an otherwise-allowed leaf read (real path) ---


def _reading_factory(called, target_path):
    """A leaf that emits a read_file(target_path) tool call; base records reach."""

    def base_dispatch(name, args):
        called["base"] = True
        return tool_result(contents="DATA")

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(
                [_tool_call_response([("c1", "read_file", {"path": target_path})]),
                 _text_response("done")]
            ),
            tool_dispatch=base_dispatch,
        )

    return factory


def test_taint_denies_an_otherwise_allowed_read_on_the_real_path(db, tmp_path):
    # A read inside fs_allow is PERMITTED untainted but DENIED tainted — proving
    # start(tainted) -> sandboxed factory -> real leaf is wired (not just the halves).
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = str(allowed / "f.txt")
    policy = WorkflowPolicy(fs_allow=(allowed,))

    # untainted: the allowed read reaches the base dispatch
    permitted = {"base": False}
    svc = WorkflowService(
        base_child_factory=_reading_factory(permitted, target), db=db, home=tmp_path, policy=policy
    )
    try:
        rid = svc.start(_SPEC, {}, tainted=False)["run_id"]
        svc.status(rid, wait=True, timeout=5)
        assert permitted["base"] is True  # allowed path permitted untainted
    finally:
        svc.shutdown()

    # tainted: the SAME read is denied before reaching base
    denied = {"base": False}
    svc2 = WorkflowService(
        base_child_factory=_reading_factory(denied, target), db=db, home=tmp_path, policy=policy
    )
    try:
        rid = svc2.start(_SPEC, {}, tainted=True)["run_id"]
        svc2.status(rid, wait=True, timeout=5)
        assert denied["base"] is False  # tainted -> denied before base
    finally:
        svc2.shutdown()
