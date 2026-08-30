"""Dogfood fixes (WF-24..WF-28) — what a real M7 run on real work exposed.

Five independent corrections, each pinned by the test that would have caught it:

- **WF-24** a manual resume silently DROPPED the run's ``args``: the tool turned
  "the caller sent no args" into ``{}`` before the service could tell, and the
  service had no fallback to the args the run persisted. A run resumed with only
  ``resume_run_id`` + ``checkpoint_answers`` then cascaded "upstream null:
  args.source" through every node that referenced an input.
- **WF-28** only ``agent`` / ``pipeline`` / ``gate`` / ``checkpoint`` cells were
  cached, so a resume RE-PAID for every ``parallel`` / ``verify`` /
  ``judge_panel`` / ``loop_until_dry`` / ``completeness_check`` node that had
  already finished (93.6k tokens re-spent on one already-done fan-out).
- **WF-25** every failure prior read "Revise: add a verify stage / schemas /
  tighter fan-out" whatever had actually gone wrong — telemetry that taught the
  next authoring nothing. The real faults are quoted now.
- **WF-26** a resumed run reported only the CURRENT segment's faults, so the
  pause that stopped the previous stretch vanished from the rollup that closes
  the run.
- **WF-27** a ``parallel`` fan-out reported no intra-node progress at all: a
  10-branch node looked identical at branch 1 and branch 9.

And what the review of those five caught in turn: an EMPTY branch slipping into
a cached fan-out (the aggregate hides it from ``cache_store``'s own guard), and
``library`` judging a resumed run on its LAST stretch alone — quoting only that
stretch's causes, and certifying a run that really failed earlier as a clean,
reusable template. A pause is exempt from that verdict: every resumed run has
one, and counting it would mean "was resumed once" = "never reusable".

Every leaf costs a deterministic 8 tokens (fake usage 5 in / 3 out); the leaf
counters are what prove a resume spawned nothing. No real sleeps.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.accounting import RunResult
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.tools import _RUN_SCHEMA, WorkflowTool
from tests.test_workflow_operability import LEAF_COST, _TWO_NODE, _ok, _service
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _counting_core(db, responder, counter, *, pool_width=4):
    """A core whose every leaf bumps ``counter`` when it really runs."""

    class CountingClient(ScriptedClient):
        def create(self, **kwargs):
            counter[0] += 1
            return super().create(**kwargs)

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=CountingClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _run(db, spec_dict, responder, run_id, counter, args=None):
    """One engine run over ``run_id``'s node cache, on a FRESH core (so nothing
    but the cache can explain a leaf that did not re-run)."""
    core = _counting_core(db, responder, counter)
    try:
        engine = WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, run_id))
        return engine.run(validate_spec(spec_dict), args or {})
    finally:
        core.shutdown()


def _run_then_resume(db, spec_dict, responder, run_id, args=None, resume_spec=None):
    """(r1, leaves_after_1, r2, leaves_after_2) — run it, then resume the same
    run_id. A cached node type leaves the second count untouched."""
    counter = [0]
    first = _run(db, spec_dict, responder, run_id, counter, args)
    after_first = counter[0]
    second = _run(db, resume_spec or spec_dict, responder, run_id, counter, args)
    return first, after_first, second, counter[0]


_PARALLEL = {
    "meta": {"name": "par", "version": 1},
    "nodes": [{"id": "p", "type": "parallel", "branches": ["alpha", "beta"]}],
}
_VERIFY = {
    "meta": {"name": "ver", "version": 1},
    "nodes": [
        {
            "id": "v",
            "type": "verify",
            "finding": "the sky is blue",
            "skeptics": 2,
            "lenses": ["optics", "evidence"],
        }
    ],
}
_PANEL = {
    "meta": {"name": "pan", "version": 1},
    "nodes": [
        {
            "id": "j",
            "type": "judge_panel",
            "attempts": ["one", "two"],
            "judges": 1,
            "synthesize": {"prompt": "merge them"},
        }
    ],
}
_LOOP = {
    "meta": {"name": "loop", "version": 1},
    "nodes": [
        {
            "id": "l",
            "type": "loop_until_dry",
            "body": {"prompt": "harvest round ${round}"},
            "max_rounds": 2,
            "stop_after_k_empty": 1,
        }
    ],
}
_COMPLETENESS = {
    "meta": {"name": "comp", "version": 1},
    "nodes": [{"id": "c", "type": "completeness_check", "task": "cover it", "results": "so far"}],
}

_REFUTED_NO = '{"refuted": false}'
_SCORE = '{"score": 1}'
_COMPLETE = '{"complete": true, "missing": []}'


def _verdict(_prompt):
    return _REFUTED_NO


def _score(_prompt):
    return _SCORE


def _complete(_prompt):
    return _COMPLETE


# --- WF-24: a resume rehydrates the run's args --------------------------


_ARGS_CHECKPOINT = {
    "meta": {"name": "argsdemo", "version": 1},
    "nodes": [
        {"id": "ask", "type": "checkpoint", "prompt": "proceed?"},
        {"id": "use", "type": "agent", "prompt": "use ${args.source} after ${ask}"},
    ],
}


class _RecordingService:
    """Just enough WorkflowService to see exactly what the tool forwarded."""

    def __init__(self):
        self.calls = []

    def start(self, spec=None, args=None, **kwargs):
        self.calls.append({"spec": spec, "args": args, **kwargs})
        return {"run_id": "r1", "status": "started"}


def test_the_tool_forwards_absent_args_as_none_not_an_empty_dict():
    # `or {}` here is what erased the difference between "no args sent" and
    # "run with no args" before the service could act on it.
    service = _RecordingService()
    WorkflowTool(service).run({"resume_run_id": "r1"})
    assert service.calls[0]["args"] is None


def test_the_tool_still_forwards_the_args_it_was_given():
    service = _RecordingService()
    WorkflowTool(service).run({"spec": _TWO_NODE, "args": {"x": 1}})
    assert service.calls[0]["args"] == {"x": 1}


def test_the_tool_refuses_args_that_are_not_an_object():
    service = _RecordingService()
    out = WorkflowTool(service).run({"spec": _TWO_NODE, "args": "nope"})
    assert "must be an object" in out and service.calls == []


def test_the_run_schema_says_a_resume_replays_the_runs_own_args():
    described = _RUN_SCHEMA["parameters"]["properties"]["args"]["description"]
    assert "resume" in described.lower()


def test_a_manual_resume_replays_the_runs_persisted_args(db, tmp_path):
    prompts = []

    def responder(prompt):
        prompts.append(prompt)
        return "R"

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_ARGS_CHECKPOINT, {"source": "dump.txt"})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        assert svc.status(run_id)["status"] == "paused"
        prompts.clear()
        # Exactly the dogfood call: only the run id and the human's answer.
        out = svc.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out
        rollup = svc.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        assert any("dump.txt" in prompt for prompt in prompts)
        assert not any("upstream null" in fault for fault in rollup["faults"])
    finally:
        svc.shutdown()


def test_explicit_args_on_a_resume_beat_the_persisted_ones(db, tmp_path):
    prompts = []

    def responder(prompt):
        prompts.append(prompt)
        return "R"

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_ARGS_CHECKPOINT, {"source": "dump.txt"})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        prompts.clear()
        svc.start(
            args={"source": "other.txt"},
            resume_run_id=run_id,
            checkpoint_answers={"ask": "yes"},
        )
        svc.status(run_id, wait=True, timeout=10)
        assert any("other.txt" in prompt for prompt in prompts)
        assert not any("dump.txt" in prompt for prompt in prompts)
    finally:
        svc.shutdown()


def test_a_fresh_run_without_args_still_runs_with_none(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


# --- WF-28: the fan-out / rigor node types replay from cache ------------


def test_a_parallel_node_replays_whole_instead_of_re_paying(db):
    r1, n1, r2, n2 = _run_then_resume(db, _PARALLEL, _ok, "run-par")
    assert r1.outputs["p"] == ["R", "R"] and n1 == 2
    assert r2.outputs["p"] == ["R", "R"] and n2 == 2  # zero new leaves


def test_a_parallel_node_with_a_dead_branch_is_never_cached(db):
    # Half a fan-out cached would read back on the resume as a finished node,
    # freezing the null in — the branch that died has to get another chance.
    def responder(prompt):
        if "beta" in prompt:
            raise RuntimeError("branch boom")
        return "R"

    r1, n1, _r2, n2 = _run_then_resume(db, _PARALLEL, responder, "run-par-dead")
    assert r1.outputs["p"] == ["R", None]
    assert n2 > n1  # the whole node re-ran


def test_a_parallel_cell_is_identified_by_its_resolved_branches(db):
    changed = {
        "meta": {"name": "par", "version": 1},
        "nodes": [{"id": "p", "type": "parallel", "branches": ["alpha", "gamma"]}],
    }
    _r1, n1, _r2, n2 = _run_then_resume(db, _PARALLEL, _ok, "run-par-id", resume_spec=changed)
    assert n2 > n1  # different branches -> a different cell, as it should be


def test_a_verify_node_replays_whole_instead_of_re_paying(db):
    r1, n1, r2, n2 = _run_then_resume(db, _VERIFY, _verdict, "run-ver")
    assert r1.outputs["v"]["survived"] is True and r1.outputs["v"]["skeptics"] == 2
    assert n1 == 2
    assert r2.outputs["v"] == r1.outputs["v"] and n2 == 2


def test_a_refuted_finding_is_a_completion_and_is_cached(db):
    def refutes(_prompt):
        return '{"refuted": true, "reason": "no"}'

    r1, n1, r2, n2 = _run_then_resume(db, _VERIFY, refutes, "run-ver-killed")
    assert r1.outputs["v"]["survived"] is False  # a verdict, not a failure
    assert r2.outputs["v"] == r1.outputs["v"] and n2 == n1


def test_a_verify_with_a_dead_skeptic_is_never_cached(db):
    def responder(prompt):
        if "evidence" in prompt:
            raise RuntimeError("skeptic died")
        return _REFUTED_NO

    r1, n1, _r2, n2 = _run_then_resume(db, _VERIFY, responder, "run-ver-dead")
    assert r1.outputs["v"]["skeptics"] == 1  # only one verdict was really read
    assert n2 > n1


def test_a_judge_panel_replays_whole_instead_of_re_paying(db):
    r1, n1, r2, n2 = _run_then_resume(db, _PANEL, _score, "run-pan")
    assert n1 == 5  # two attempts + one judge each + the synthesis
    assert r2.outputs["j"] == r1.outputs["j"] and n2 == 5


def test_a_judge_panel_with_a_dead_attempt_is_never_cached(db):
    def responder(prompt):
        if "two" in prompt and "score" not in prompt:
            raise RuntimeError("attempt died")
        return _SCORE

    _r1, n1, _r2, n2 = _run_then_resume(db, _PANEL, responder, "run-pan-dead")
    assert n2 > n1


def test_a_loop_until_dry_replays_whole_instead_of_re_paying(db):
    r1, n1, r2, n2 = _run_then_resume(db, _LOOP, _ok, "run-loop")
    assert r1.outputs["l"] == ["R", "R"] and n1 == 2
    assert r2.outputs["l"] == ["R", "R"] and n2 == 2


def test_a_loop_with_a_dead_round_is_never_cached(db):
    def responder(prompt):
        if "round 1" in prompt:
            raise RuntimeError("round died")
        return "R"

    r1, n1, _r2, n2 = _run_then_resume(db, _LOOP, responder, "run-loop-dead")
    assert r1.outputs["l"] == ["R"]
    assert n2 > n1


def test_a_completeness_check_replays_instead_of_re_paying(db):
    r1, n1, r2, n2 = _run_then_resume(db, _COMPLETENESS, _complete, "run-comp")
    assert r1.outputs["c"] == {"complete": True, "missing": []} and n1 == 1
    assert r2.outputs["c"] == r1.outputs["c"] and n2 == 1


def test_a_cached_fan_out_is_stored_with_what_the_whole_node_cost(db):
    # The cell replays for free, so its row has to carry the price of EVERY leaf
    # the node paid for — otherwise a resumed run's ceiling forgets the fan-out.
    counter = [0]
    _run(db, _PARALLEL, _ok, "run-cost", counter)
    assert NodeCache(db, "run-cost").total_cost() == (2 * 5, 2 * 3)


# --- WF-27: a parallel fan-out reports its progress ---------------------


def test_a_parallel_node_publishes_items_as_its_branches_collect(db):
    counter = [0]
    calls = []
    core = _counting_core(db, _ok, counter)
    engine = WorkflowEngine(core, budget=Budget())
    engine.note_node_items = lambda node_id, done, total: calls.append((node_id, done, total))
    try:
        engine.run(validate_spec(_PARALLEL), {})
    finally:
        core.shutdown()
    assert calls == [("p", 0, 2), ("p", 1, 2), ("p", 2, 2)]


def test_a_finished_parallel_node_reports_every_item_settled(db):
    counter = [0]
    core = _counting_core(db, _ok, counter)
    engine = WorkflowEngine(core, budget=Budget())
    try:
        engine.run(validate_spec(_PARALLEL), {})
    finally:
        core.shutdown()
    node = next(n for n in engine.progress_snapshot()["nodes"] if n["id"] == "p")
    assert node["items"] == {"done": 2, "total": 2}


# --- WF-25: a prior quotes the cause, not a slogan ----------------------


_SPEC_FOR_PRIOR = {"meta": {"name": "triage"}, "nodes": [{"id": "a", "type": "agent"}]}


def test_a_problematic_run_quotes_nothing_anywhere(tmp_path):
    """Legacy automatic insight-writing is OFF: a degraded run — with faults,
    without them, with a giant traceback — publishes nothing and saves no
    template. The legacy file, if present, is left byte-identical."""
    legacy = tmp_path / "workflows" / "insights.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("- [kept] shape a -> degraded.\n", encoding="utf-8")
    before = legacy.read_bytes()
    for faults in (
        ["use: upstream null: args.source", "v: all skeptics dead (fail-closed)"],
        [],
        ["a: broke\nsecond line " + "y" * 400 + " TAIL"],
    ):
        result = RunResult(status="degraded", nodes_total=2, null_count=1, faults=list(faults))
        library.record_outcome(tmp_path, _SPEC_FOR_PRIOR, result)
        assert library.recent_insights(tmp_path) == []
    assert legacy.read_bytes() == before  # read-only
    assert library.list_templates(tmp_path) == []


# --- WF-26: a resumed run still reports the faults it already had -------


def test_a_resumed_run_reports_the_earlier_segments_faults(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)
        assert first["status"] == "paused"
        assert first["faults"] == [f"token budget exhausted: spent {LEAF_COST} of 5 tokens"]
        # One segment: the two lists are the same list, so only one is reported.
        assert "faults_total" not in first
        svc.start(resume_run_id=run_id, token_budget=200)
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert out["faults"] == []  # this stretch really was clean
        assert out["faults_total"] == first["faults"]  # the pause is still visible
    finally:
        svc.shutdown()


def test_a_single_segment_run_reports_one_fault_list(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["faults"] == [] and "faults_total" not in out
    finally:
        svc.shutdown()


# --- WF-28 (cont.): an EMPTY branch is a non-completion too --------------


def test_a_parallel_branch_that_answered_nothing_is_never_cached(db):
    # "" is the same kind of non-completion as a dead leaf (WF-7) — it just
    # hides better: it is not None, so a per-list gate waves the whole fan-out
    # through and the silence is frozen into every later stretch of the run.
    def responder(prompt):
        return "" if "beta" in prompt else "R"

    r1, n1, _r2, n2 = _run_then_resume(db, _PARALLEL, responder, "run-par-empty")
    assert r1.outputs["p"] == ["R", ""]
    assert n2 > n1  # the silent branch got another chance


# --- WF-25/26 (cont.): the LIBRARY learns from the whole run ------------


_THREE_NODE = {
    "meta": {"name": "stretched", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
        # No ref into b (an upstream null would short-circuit the spawn and the
        # run would never reach the budget gate), only the ordering.
        {"id": "c", "type": "agent", "prompt": "solo", "depends_on": ["b"]},
    ],
}


def test_a_prior_quotes_the_faults_of_every_stretch_not_just_the_last(db, tmp_path):
    # Stretch 1 pauses on the token budget; stretch 2 fails on its own. The
    # prior is what the next authoring reads — it must carry both causes.
    def responder(prompt):
        return "" if "then" in prompt else "R"

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(resume_run_id=run_id, token_budget=200)
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "degraded"
    finally:
        svc.shutdown()
    assert library.recent_insights(tmp_path) == []  # legacy learning is off


def test_a_run_that_faulted_in_an_earlier_stretch_is_no_template(db, tmp_path):
    # The run is ONE run: a stretch that really failed is a lesson about the
    # spec, and finishing the last stretch cleanly does not erase it.
    ok = {"now": False}

    def responder(prompt):
        return "R" if ok["now"] or "then" not in prompt else ""

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_THREE_NODE, {}, token_budget=20)["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)
        assert first["status"] == "paused"
        assert any("empty output" in f for f in first["faults"])
        ok["now"] = True
        svc.start(resume_run_id=run_id, token_budget=200)
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"  # the last stretch really was clean
    finally:
        svc.shutdown()
    assert library.list_templates(tmp_path) == []  # certifying this would be a lie
    assert library.recent_insights(tmp_path) == []  # and legacy learning is off


def test_a_run_that_only_ever_PAUSED_is_still_a_template(db, tmp_path):
    # The other side of the same rule: every resumed run carries a pause fault,
    # and counting that as a failure would make "was resumed once" mean "never
    # reusable" — the run below did nothing wrong but run out of ceiling.
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(resume_run_id=run_id, token_budget=200)
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()
    assert [t["name"] for t in library.list_templates(tmp_path)] == ["demo"]
