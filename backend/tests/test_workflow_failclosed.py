"""Fail-closed semantics of the workflow engine (CC-parity, fatia A).

A harness that hides its own failures is worse than one that fails loudly: the
author reads a green run and certifies a broken template. These tests pin the
five places where the engine used to fail OPEN — a container ref that quietly
became ``[]``, an upstream null baked into a leaf prompt as the literal "null",
a dead leaf whose cause was dropped, a dead loop round counted as "dry", and a
verify whose skeptics all died being read as "survived".
"""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


class ScriptedClient(ModelClient):
    """Replies based on the prompt text; a responder that raises kills the leaf."""

    def __init__(self, responder):
        self._responder = responder

    def _prompt(self, kwargs):
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        return _text_response(self._responder(self._prompt(kwargs)))

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _run(core, spec_dict, args=None):
    spec = validate_spec(spec_dict)
    assert not hasattr(spec, "issues"), getattr(spec, "message", "")  # spec must validate
    return WorkflowEngine(core, budget=Budget()).run(spec, args or {})


def _faults(result):
    return " | ".join(result.faults)


# --- WF-12: a container ref that isn't a list is an error, never a silent [] ---


def test_parallel_branches_ref_to_non_list_faults(db):
    seen = []
    core = _core(db, lambda p: seen.append(p) or "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "gen", "type": "agent", "prompt": "make"},
                {"id": "par", "type": "parallel", "branches": "${gen}"},  # resolves to a string
            ],
        })
        assert result.outputs["par"] is None  # NOT an empty list of results
        assert "branches resolved to non-list" in _faults(result)
        assert len(seen) == 1  # only `gen` ran; no branch leaf was spawned
    finally:
        core.shutdown()


def test_judge_panel_attempts_ref_to_non_list_faults(db):
    core = _core(db, lambda p: "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "gen", "type": "agent", "prompt": "make"},
                {"id": "jp", "type": "judge_panel", "attempts": "${gen}", "judges": 1,
                 "synthesize": {"type": "agent", "prompt": "sum ${winner}"}},
            ],
        })
        assert result.outputs["jp"] is None
        assert "attempts resolved to non-list" in _faults(result)
    finally:
        core.shutdown()


def test_container_ref_to_null_reports_the_upstream_path(db):
    core = _core(db, lambda p: "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "par", "type": "parallel", "branches": "${args.missing}"}],
        })
        assert result.outputs["par"] is None
        assert "upstream null: args.missing" in _faults(result)
    finally:
        core.shutdown()


def test_null_branches_field_is_not_an_empty_fan_out(db):
    core = _core(db, lambda p: "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "par", "type": "parallel", "branches": None}],
        })
        assert result.outputs["par"] is None
        assert "branches resolved to non-list" in _faults(result)
    finally:
        core.shutdown()


def test_authored_branch_with_a_null_ref_fails_the_node(db):
    seen = []
    core = _core(db, lambda p: seen.append(p) or "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "par", "type": "parallel", "branches": [
                {"type": "agent", "prompt": "fine"},
                {"type": "agent", "prompt": "use ${args.missing}"},
            ]}],
        })
        assert result.outputs["par"] is None
        assert "upstream null: args.missing" in _faults(result)
        assert seen == []  # fails before ANY branch is spawned
    finally:
        core.shutdown()


def test_branches_from_a_ref_are_inert_literals(db):
    """Entries that CAME from a ref are untrusted leaf output: a ``${...}`` inside
    them must never be resolved (single-pass, §2.3) — otherwise a leaf could
    inject a second-order reference and read the run's args."""
    seen = []

    def responder(prompt):
        seen.append(prompt)
        if "make" in prompt:
            return '{"items": ["look at ${args.secret}"]}'
        return "R"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "gen", "type": "agent", "prompt": "make"},
                {"id": "par", "type": "parallel", "branches": "${gen.items}"},
            ],
        }, {"secret": "LEAKED"})
        assert result.outputs["par"] == ["R"]
        assert any("${args.secret}" in p for p in seen)  # passed through verbatim
        assert not any("LEAKED" in p for p in seen)  # never resolved
    finally:
        core.shutdown()


def test_judge_panel_with_all_judges_dead_crowns_nobody(db):
    """An unscored attempt used to average to 0.0, which ranks like a real score —
    so a panel where every judge died still crowned the first attempt."""
    def responder(prompt):
        if "Score this attempt" in prompt:
            raise RuntimeError("judge died")
        return "an attempt"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "jp", "type": "judge_panel", "judges": 2,
                       "attempts": [{"type": "agent", "prompt": "one"},
                                    {"type": "agent", "prompt": "two"}],
                       "synthesize": {"type": "agent", "prompt": "sum ${winner}"}}],
        })
        assert result.outputs["jp"] is None  # no winner without a real judgement
        assert "attempt unscored" in _faults(result)
    finally:
        core.shutdown()


# --- WF-14: an upstream null never becomes the literal "null" in a prompt ---


def test_agent_prompt_whole_null_ref_fails_the_node(db):
    seen = []
    core = _core(db, lambda p: seen.append(p) or "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "${args.missing}"}],
        })
        assert result.outputs["a"] is None
        assert "upstream null: args.missing" in _faults(result)
        assert seen == []  # never spawned
    finally:
        core.shutdown()


def test_agent_prompt_embedded_null_ref_fails_the_node(db):
    seen = []
    core = _core(db, lambda p: seen.append(p) or "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "summarize: ${args.missing}"}],
        })
        assert result.outputs["a"] is None
        assert "upstream null" in _faults(result)
        assert seen == []  # no leaf was told to summarize "null"
    finally:
        core.shutdown()


def test_verify_with_a_null_finding_verifies_nothing(db):
    core = _core(db, lambda p: '{"refuted": false}')
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "v", "type": "verify", "finding": "${args.missing}", "skeptics": 3}],
        })
        assert result.outputs["v"] is None
        assert "upstream null" in _faults(result)
    finally:
        core.shutdown()


def test_loop_body_with_a_null_ref_fails_the_node(db):
    # `gen` completes with an object that simply has no `claims` key: nothing
    # upstream is dead, so no fault is recorded there — the hole only shows up
    # when the loop body interpolates it. Every round used to be spawned with the
    # literal text "refine null" and the leaf confabulated over it.
    seen = []

    def responder(prompt):
        seen.append(prompt)
        return "{}"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "gen", "type": "agent", "prompt": "make"},
                {"id": "loop", "type": "loop_until_dry",
                 "body": {"type": "agent", "prompt": "refine ${gen.claims}"},
                 "stop_after_k_empty": 1, "max_rounds": 3},
            ],
        })
        assert result.outputs["loop"] is None  # NOT a list that reads as "dry"
        assert "upstream null: gen.claims" in _faults(result)
        assert len(seen) == 1  # only `gen` ran; no round was told to refine "null"
    finally:
        core.shutdown()


def test_pipeline_null_item_drops_only_that_item(db):
    seen = []
    core = _core(db, lambda p: seen.append(p) or "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}",
                       "stages": [{"type": "agent", "prompt": "do ${item}"}]}],
        }, {"items": ["ok", None]})
        assert result.outputs["p"] == ["R", None]  # per-item isolation preserved
        assert "upstream null" in _faults(result)
        assert len(seen) == 1  # the null item never spawned a leaf
    finally:
        core.shutdown()


# --- WF-15: a dead leaf carries its cause, and the run status tells the truth ---


def test_dead_leaf_fault_carries_the_cause(db):
    def responder(prompt):
        raise RuntimeError("leaf boom")

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go"}],
        })
        assert result.outputs["a"] is None
        assert "leaf boom" in _faults(result)  # the WHY, not just a bare null
        assert "a:" in _faults(result)  # attributed to the node
    finally:
        core.shutdown()


def test_all_null_run_is_failed(db):
    def responder(prompt):
        raise RuntimeError("leaf boom")

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go"},
                      {"id": "b", "type": "agent", "prompt": "go too"}],
        })
        assert result.null_count == 2
        assert result.status == "failed"  # nothing survived: not "complete"
    finally:
        core.shutdown()


def test_partial_null_run_is_degraded(db):
    def responder(prompt):
        if "die" in prompt:
            raise RuntimeError("leaf boom")
        return "R"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "die"},
                      {"id": "b", "type": "agent", "prompt": "live"}],
        })
        assert result.outputs == {"a": None, "b": "R"}
        assert result.status == "degraded"  # a null is never a clean run
    finally:
        core.shutdown()


def test_clean_run_stays_complete(db):
    core = _core(db, lambda p: "R")
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go"}],
        })
        assert result.status == "complete" and result.faults == []
    finally:
        core.shutdown()


# --- WF-4: a dead loop round is not evidence of dryness ---


def test_loop_dead_round_does_not_count_as_dry(db):
    # rounds: 0 -> a find, 1 -> DEAD, 2 -> empty, 3 -> a find. With stop_after_k_empty=2
    # the old code counted the dead round as empty and stopped at round 2, losing "B".
    def responder(prompt):
        if "find 1" in prompt:
            raise RuntimeError("round died")
        if "find 0" in prompt:
            return "A"
        if "find 2" in prompt:
            return ""
        return "B"

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "loop", "type": "loop_until_dry",
                       "body": {"type": "agent", "prompt": "find ${round}"},
                       "stop_after_k_empty": 2, "max_rounds": 4}],
        })
        assert result.outputs["loop"] == ["A", "B"]
        assert "round" in _faults(result)  # the dead round is visible, not silent
    finally:
        core.shutdown()


def test_loop_still_stops_on_real_empties(db):
    replies = iter(["found", "", ""])
    core = _core(db, lambda p: next(replies, ""))
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "loop", "type": "loop_until_dry",
                       "body": {"type": "agent", "prompt": "find ${round}"},
                       "stop_after_k_empty": 2, "max_rounds": 6}],
        })
        assert result.outputs["loop"] == ["found"]  # "" still means dry
    finally:
        core.shutdown()


# --- WF-18: zero live skeptics never approves a finding ---


def test_verify_with_all_skeptics_dead_does_not_survive(db):
    def responder(prompt):
        raise RuntimeError("skeptic died")

    core = _core(db, responder)
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "v", "type": "verify", "finding": "${args.claim}", "skeptics": 3}],
        }, {"claim": "the sky is green"})
        out = result.outputs["v"]
        assert out["survived"] is False  # unverified is NOT verified
        assert out["finding"] is None
        assert out["skeptics"] == 0
        assert "all skeptics dead" in _faults(result)
    finally:
        core.shutdown()


def test_verify_with_live_skeptics_still_survives(db):
    core = _core(db, lambda p: '{"refuted": false}')
    try:
        result = _run(core, {
            "meta": {"name": "x"},
            "nodes": [{"id": "v", "type": "verify", "finding": "${args.claim}", "skeptics": 3}],
        }, {"claim": "water is wet"})
        out = result.outputs["v"]
        assert out["survived"] is True and out["finding"] == "water is wet"
    finally:
        core.shutdown()
