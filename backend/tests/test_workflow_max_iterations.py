"""Configurable ``max_iterations`` — the leash the author could not reach.

``timeout`` has been a per-node knob since M4; the iteration cap was not, so a
leaf that needed one more tool round than the fixed cap allowed died as a null
with nothing the spec author could do about it. Three surfaces, one knob:

- per NODE (and per pipeline STAGE): ``max_iterations``, capped + validated at
  author time, part of the cell identity (change it and the cell re-runs);
- per SESSION: ``--max-iterations`` / ``LOHRA_MAX_ITERATIONS`` for the main
  agent (flag > env > default, the ``resolve_limits`` precedence);
- per SUBAGENT: ``max_iterations`` on ``delegate_task`` / ``spawn_session``,
  beside the ``model``/``effort`` overrides already there.

Honesty gate: a leaf that BUSTS the cap must reach the rollup with the cause
named ("max_iterations (N) reached"), never a mute null.

Every leaf here burns iterations with ``pause_turn`` responses (the provider
suspending a turn) — the one deterministic way to make the loop go around
again without a real tool. Counts are exact: the client is programmed with a
known number of pauses, so "died at the cap" and "finished under it" are the
only two outcomes.
"""

import json

import pytest

from lohra.agent.agent import DEFAULT_MAX_ITERATIONS, Agent
from lohra.agent.client import ModelClient
from lohra.agent.client_pool import configure_for
from lohra.agent.delegate import CHILD_MAX_ITERATIONS, DelegateTaskTool
from lohra.agent.limits import ENV_MAX_ITERATIONS, resolve_max_iterations
from lohra.agent.overrides import make_configure
from lohra.orchestration.core import OrchestrationCore
from lohra.orchestration.tools import OrchestrationTool
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.nodes import (
    DEFAULT_LEAF_MAX_ITERATIONS,
    MAX_NODE_MAX_ITERATIONS,
    Node,
    node_max_iterations,
)
from lohra.workflow.schema import ValidationError, validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class _PausingClient(ModelClient):
    """Answers ``pauses`` suspended turns, then one real answer.

    A ``pause_turn`` is terminal for nothing: the loop resends and burns one
    more iteration. That is the deterministic iteration burner (a tool_calls
    response would need a dispatch, and without one the loop treats it as the
    end of the turn)."""

    def __init__(self, pauses: int, text: str = "done") -> None:
        self._left = pauses
        self._text = text
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            return _text_response("still working", stop_reason="pause_turn")
        return _text_response(self._text)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _pausing_core(db, pauses: int, text: str = "done", *, pool_width: int = 4):
    """A core whose every leaf burns ``pauses`` iterations before answering."""

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=_PausingClient(pauses, text),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _recording_core(db, responder):
    """A core that hands back every Agent it built (to inspect its knobs)."""
    built: list[Agent] = []

    def factory():
        agent = Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=_ScriptedClient(responder),
        )
        built.append(agent)
        return agent

    return OrchestrationCore(db, factory), built


class _ScriptedClient(ModelClient):
    def __init__(self, responder):
        self._responder = responder

    def create(self, **kwargs):
        msgs = kwargs.get("messages") or []
        prompt = " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))
        return _text_response(self._responder(prompt))

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)


def _agent_spec(**fields):
    node = {"id": "a", "type": "agent", "prompt": "go"}
    node.update(fields)
    return validate_spec({"meta": {"name": "iters"}, "nodes": [node]})


def _engine(core, **kwargs):
    return WorkflowEngine(core, budget=Budget(), **kwargs)


def _faults(result):
    return "\n".join(result.faults)


# --- author-time validation: a declared knob is never silently ignored -------


def test_agent_node_accepts_max_iterations():
    spec = _agent_spec(max_iterations=24)
    assert not isinstance(spec, ValidationError)
    assert spec.nodes[0].fields["max_iterations"] == 24


def test_max_iterations_at_the_cap_is_accepted():
    assert not isinstance(_agent_spec(max_iterations=MAX_NODE_MAX_ITERATIONS), ValidationError)


@pytest.mark.parametrize(
    "value", [0, -1, MAX_NODE_MAX_ITERATIONS + 1, 1.5, "ten", True, None, [12]]
)
def test_invalid_max_iterations_is_a_didactic_issue(value):
    spec = _agent_spec(max_iterations=value)
    assert isinstance(spec, ValidationError)
    assert "max_iterations" in spec.message
    assert str(MAX_NODE_MAX_ITERATIONS) in spec.message  # says WHERE the ceiling is
    assert "e.g." in spec.message  # didactic: it SHOWS the fix


# --- the runtime getter stays lenient (a stage dict must never crash a run) --


def test_node_max_iterations_falls_back_and_clamps():
    assert node_max_iterations({}, 50) == 50
    assert node_max_iterations({"max_iterations": "nope"}, 50) == 50
    assert node_max_iterations({"max_iterations": True}, 50) == 50
    assert node_max_iterations({"max_iterations": 0}, 50) == 50
    assert node_max_iterations({"max_iterations": 12}, 50) == 12
    # never unbounded, even if a malformed spec reached the runtime
    assert node_max_iterations({"max_iterations": 10_000}, 50) == MAX_NODE_MAX_ITERATIONS


# --- the leaf default: its own, higher than the chat default (pinned) -------


def test_workflow_leaf_default_is_higher_than_the_chat_default():
    # The chat default (8) is for a tool-less turn; a workflow leaf works, so it
    # inherits the delegated-subagent cap. Pinned so the two can never drift.
    assert DEFAULT_LEAF_MAX_ITERATIONS == CHILD_MAX_ITERATIONS
    assert DEFAULT_LEAF_MAX_ITERATIONS > DEFAULT_MAX_ITERATIONS


# --- the knob reaches the leaf ---------------------------------------------


def test_make_configure_sets_max_iterations():
    agent = type("A", (), {"model": "m", "effort": None, "forced_tool": None,
                           "max_iterations": 8})()
    configure = make_configure(max_iterations=32)
    assert configure is not None  # the ONLY override must not be dropped
    configure(agent)
    assert agent.max_iterations == 32
    assert agent.model == "m" and agent.effort is None  # nothing else touched


def test_make_configure_still_none_without_any_override():
    assert make_configure() is None
    assert make_configure(max_iterations=None) is None


def test_configure_for_passes_max_iterations():
    agent = type("A", (), {"model": "m", "effort": None, "forced_tool": None,
                           "max_iterations": 8})()
    configure_for(None, max_iterations=32)(agent)
    assert agent.max_iterations == 32


def _configure_for_node(node):
    from lohra.workflow.strategies import _leaf_config, _node_configure

    engine = type("E", (), {"tiers": None})()
    model, effort, provider, _warning = _leaf_config(engine, node)
    return _node_configure(node, None, None, model, effort, provider)


def test_workflow_node_max_iterations_applied_to_leaf():
    node = Node(id="a", type="agent", fields={"prompt": "x", "max_iterations": 32})
    configure = _configure_for_node(node)
    assert configure is not None
    agent = type("A", (), {"model": "orch", "effort": None, "forced_tool": None,
                           "max_iterations": 8})()
    configure(agent)
    assert agent.max_iterations == 32


def test_node_without_knobs_builds_no_configure():
    # Byte-identical default: no override -> no hook at all.
    assert _configure_for_node(Node(id="a", type="agent", fields={"prompt": "x"})) is None


# --- end to end: the cap actually bounds the leaf ---------------------------


def test_node_max_iterations_lets_a_long_leaf_finish(db):
    # 9 suspended turns + 1 answer = 10 calls: over the leaf's built-in 8, under
    # the 12 this node asked for.
    core = _pausing_core(db, pauses=9, text="finished")
    try:
        result = _engine(core).run(_agent_spec(max_iterations=12), {})
        assert result.outputs["a"] == "finished"
        assert "max_iterations" not in _faults(result)
    finally:
        core.shutdown()


def test_node_max_iterations_bounds_a_runaway_leaf_with_a_named_fault(db):
    core = _pausing_core(db, pauses=9, text="finished")
    try:
        result = _engine(core).run(_agent_spec(max_iterations=3), {})
        assert result.outputs["a"] is None
        # Honesty: the rollup names the cause and the number that was hit.
        assert "max_iterations (3) reached" in _faults(result)
        assert "a: leaf error" in _faults(result)
    finally:
        core.shutdown()


def test_without_the_knob_the_leaf_keeps_the_factory_cap(db):
    # No field -> no configure -> the child factory's own cap decides (here the
    # bare Agent default of 8, which 9 pauses busts).
    core = _pausing_core(db, pauses=9, text="finished")
    try:
        result = _engine(core).run(_agent_spec(), {})
        assert result.outputs["a"] is None
        assert f"max_iterations ({DEFAULT_MAX_ITERATIONS}) reached" in _faults(result)
    finally:
        core.shutdown()


# --- cell identity: change the leash, re-run the cell -----------------------


def test_max_iterations_change_invalidates_the_cached_cell(db):
    prompts: list[str] = []
    core, _built = _recording_core(db, lambda prompt: (prompts.append(prompt), "R")[1])
    try:
        cache = NodeCache(db, "run-1")
        assert _engine(core, cache=cache).run(_agent_spec(max_iterations=12), {}).outputs["a"] == "R"
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_agent_spec(max_iterations=12), {})  # same cell: replayed
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_agent_spec(max_iterations=13), {})  # new identity
        assert len(prompts) == 2
    finally:
        core.shutdown()


# --- pipeline stages get the same knob --------------------------------------


def _pipeline_spec(stage_fields):
    stage = {"type": "agent", "prompt": "work on ${item}"}
    stage.update(stage_fields)
    return validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [{"id": "p", "type": "pipeline", "items": ["x"], "stages": [stage]}],
        }
    )


def test_pipeline_stage_honours_max_iterations(db):
    core = _pausing_core(db, pauses=9, text="staged")
    try:
        result = _engine(core).run(_pipeline_spec({"max_iterations": 12}), {})
        assert result.outputs["p"] == ["staged"]
    finally:
        core.shutdown()


def test_pipeline_stage_without_the_knob_keeps_the_factory_cap(db):
    core = _pausing_core(db, pauses=9, text="staged")
    try:
        result = _engine(core).run(_pipeline_spec({}), {})
        assert result.outputs["p"] == [None]
        assert "max_iterations" in _faults(result)
    finally:
        core.shutdown()


def test_stage_max_iterations_is_part_of_the_cell_identity(db):
    prompts: list[str] = []
    core, _built = _recording_core(db, lambda prompt: (prompts.append(prompt), "R")[1])
    try:
        cache = NodeCache(db, "run-p")
        assert _engine(core, cache=cache).run(_pipeline_spec({"max_iterations": 12}), {}).outputs[
            "p"
        ] == ["R"]
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_pipeline_spec({"max_iterations": 12}), {})  # replayed
        assert len(prompts) == 1
        _engine(core, cache=cache).run(_pipeline_spec({"max_iterations": 13}), {})  # re-spawned
        assert len(prompts) == 2
    finally:
        core.shutdown()


# --- delegate_task / spawn_session ------------------------------------------


def test_spawn_session_passes_max_iterations(db):
    core, built = _recording_core(db, lambda prompt: "ok")
    try:
        out = json.loads(OrchestrationTool(core).spawn({"prompt": "go", "max_iterations": 32}))
        assert out["ok"] is True
        core.collect(out["sub_id"], wait=True, timeout=5)
        assert built[-1].max_iterations == 32
    finally:
        core.shutdown()


def test_spawn_session_rejects_an_out_of_range_max_iterations(db):
    core, _built = _recording_core(db, lambda prompt: "ok")
    try:
        out = json.loads(OrchestrationTool(core).spawn({"prompt": "go", "max_iterations": 0}))
        assert "error" in out and "max_iterations" in out["error"]
        over = json.loads(
            OrchestrationTool(core).spawn(
                {"prompt": "go", "max_iterations": MAX_NODE_MAX_ITERATIONS + 1}
            )
        )
        assert "error" in over and str(MAX_NODE_MAX_ITERATIONS) in over["error"]
    finally:
        core.shutdown()


def test_delegate_task_passes_max_iterations(db):
    core, built = _recording_core(db, lambda prompt: "ok")
    try:
        out = json.loads(
            DelegateTaskTool(core).handle({"tasks": ["do a thing"], "max_iterations": 32})
        )
        assert out["ok"] is True
        assert built[-1].max_iterations == 32
    finally:
        core.shutdown()


def test_delegate_task_rejects_an_out_of_range_max_iterations(db):
    core, _built = _recording_core(db, lambda prompt: "ok")
    try:
        out = json.loads(DelegateTaskTool(core).handle({"tasks": ["t"], "max_iterations": -3}))
        assert "error" in out and "max_iterations" in out["error"]
    finally:
        core.shutdown()


def test_spawn_without_the_knob_keeps_the_factory_cap(db):
    core, built = _recording_core(db, lambda prompt: "ok")
    try:
        out = json.loads(OrchestrationTool(core).spawn({"prompt": "go"}))
        core.collect(out["sub_id"], wait=True, timeout=5)
        assert built[-1].max_iterations == DEFAULT_MAX_ITERATIONS  # untouched
    finally:
        core.shutdown()


# --- per session: flag > env > default (the resolve_limits precedence) ------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV_MAX_ITERATIONS, raising=False)


def test_max_iterations_default_when_nothing_set():
    assert resolve_max_iterations(default=90) == 90
    assert resolve_max_iterations(default=DEFAULT_MAX_ITERATIONS) == DEFAULT_MAX_ITERATIONS


def test_env_overrides_the_default(monkeypatch):
    monkeypatch.setenv(ENV_MAX_ITERATIONS, "200")
    assert resolve_max_iterations(default=90) == 200


def test_flag_overrides_the_env(monkeypatch):
    monkeypatch.setenv(ENV_MAX_ITERATIONS, "200")
    assert resolve_max_iterations(override=12, default=90) == 12


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-4", ""])
def test_invalid_env_falls_back_to_the_default(monkeypatch, raw):
    monkeypatch.setenv(ENV_MAX_ITERATIONS, raw)
    assert resolve_max_iterations(default=90) == 90


def test_flag_is_clamped_to_at_least_one():
    assert resolve_max_iterations(override=0, default=90) == 1


def test_cli_parses_the_max_iterations_flag():
    from lohra.cli import build_parser

    args = build_parser().parse_args(["chat", "hi", "--max-iterations", "200"])
    assert args.max_iterations == 200
    # absent -> None, so the resolver falls through to env/default
    assert build_parser().parse_args(["chat", "hi"]).max_iterations is None


# --- backward compatibility: a knob-less cell keeps its PRE-knob identity ----
#
# `workflow_node_cache` is persisted and run-scoped, and `resume_run_id` replays
# it across process restarts — including across an upgrade of Lohra itself. So
# adding a knob must NOT re-key the cells that never used it: a run cached
# before `max_iterations` existed, resumed after, must still HIT its rows
# instead of silently re-spawning (and re-billing) every leaf.


def test_a_knobless_agent_cell_keeps_its_pre_knob_hash(db):
    from lohra.workflow.prompts import strict_prompt
    from lohra.workflow.strategies import _leaf_config

    prompts: list[str] = []
    core, _built = _recording_core(db, lambda prompt: (prompts.append(prompt), "R")[1])
    try:
        cache = NodeCache(db, "run-legacy")
        engine = _engine(core, cache=cache)
        spec = _agent_spec()  # declares no max_iterations
        assert engine.run(spec, {}).outputs["a"] == "R"
        node = spec.nodes[0]
        model, effort, provider, _warning = _leaf_config(engine, node)
        legacy = engine.cell_hash(
            node.id,
            "agent",
            strict_prompt(engine, node.id, node.fields.get("prompt", ""), {}),
            engine.resolve_schema(node.fields),
            model,
            effort,
            provider,
            node.fields.get("timeout"),
            node.fields.get("retries"),
        )
        assert db.cache_get("run-legacy", legacy) is not None
    finally:
        core.shutdown()


def test_a_knobless_pipeline_stage_keeps_its_pre_knob_hash(db):
    from lohra.workflow.prompts import strict_prompt

    core, _built = _recording_core(db, lambda prompt: "R")
    try:
        cache = NodeCache(db, "run-legacy-p")
        engine = _engine(core, cache=cache)
        spec = _pipeline_spec({})  # stage declares no max_iterations
        assert engine.run(spec, {}).outputs["p"] == ["R"]
        stage = spec.nodes[0].fields["stages"][0]
        stage_ctx = {"item": "x", "stage": {"result": "x"}}  # stage 0's prev IS the item
        legacy = engine.cell_hash(
            "p",
            0,
            "x",
            strict_prompt(engine, "p#0#0", stage["prompt"], stage_ctx),
            engine.resolve_schema(stage),
        )
        assert db.cache_get("run-legacy-p", legacy) is not None
    finally:
        core.shutdown()
