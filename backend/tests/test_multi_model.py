"""Tests for per-spawn model + reasoning effort overrides (same provider)."""

from lohra.agent.overrides import make_configure
from lohra.providers.transports import get_transport


# --- make_configure ---


def test_make_configure_none_when_empty():
    assert make_configure() is None


def test_make_configure_sets_model_and_effort():
    agent = type("A", (), {"model": "orch", "effort": None, "forced_tool": None})()
    make_configure(model="sub-model", effort="high")(agent)
    assert agent.model == "sub-model" and agent.effort == "high"


def test_make_configure_leaves_unset_fields():
    agent = type("A", (), {"model": "orch", "effort": "low", "forced_tool": None})()
    make_configure(model="sub")(agent)  # only model
    assert agent.model == "sub" and agent.effort == "low"  # effort untouched


# --- effort emitted by the right transports (None = byte-identical default) ---


def test_responses_emits_reasoning_effort():
    t = get_transport("responses")
    kw = t.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], effort="xhigh")
    # ``summary`` rides along with effort since #59 (the abort window during
    # reasoning); effort itself is unchanged and never sent on its own path.
    assert kw["reasoning"]["effort"] == "xhigh"
    assert "reasoning" not in t.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}])


def test_chat_completions_emits_reasoning_effort():
    t = get_transport("chat_completions")
    kw = t.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], effort="high")
    assert kw["reasoning_effort"] == "high"
    assert "reasoning_effort" not in t.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}])


def test_anthropic_effort_is_noop():
    t = get_transport("anthropic_messages")
    kw = t.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], effort="high")
    # accepted (ABC contract) but not emitted — Anthropic effort shape is version-specific
    assert "reasoning" not in kw and "reasoning_effort" not in kw and "effort" not in kw


def test_default_effort_byte_identical():
    for mode in ("responses", "chat_completions", "anthropic_messages"):
        t = get_transport(mode)
        args = dict(model="m", messages=[{"role": "user", "content": "x"}])
        assert t.build_kwargs(**args) == t.build_kwargs(**args, effort=None)


# --- workflow agent node: model + effort reach the leaf via configure (was a no-op) ---


def _configure_for_node(node):
    """The two halves run_agent runs: resolve the node's config (folding in any
    model tier, WF-5), then build the configure hook from the RESOLVED values."""
    from lohra.workflow.strategies import _leaf_config, _node_configure

    engine = type("E", (), {"tiers": None})()
    model, effort, provider, _warning = _leaf_config(engine, node)
    return _node_configure(node, None, None, model, effort, provider)


def test_workflow_node_model_and_effort_applied_to_leaf():
    from lohra.workflow.nodes import Node

    node = Node(id="a", type="agent",
                fields={"prompt": "x", "model": "sub-model", "effort": "high"})
    configure = _configure_for_node(node)
    assert configure is not None
    agent = type("A", (), {"model": "orch", "effort": None, "forced_tool": None})()
    configure(agent)
    assert agent.model == "sub-model" and agent.effort == "high"


def test_workflow_node_without_overrides_no_configure():
    from lohra.workflow.nodes import Node

    node = Node(id="a", type="agent", fields={"prompt": "x"})
    assert _configure_for_node(node) is None  # nothing to override


def test_provider_unavailable_is_a_named_fault_not_a_silent_null():
    # Fail-isolation must stay LOUD: when the node's provider override cannot be
    # resolved (no pool here), the leaf drops to null AND the rollup names the
    # cause — a bare None with no fault reads as "ran and said nothing".
    from lohra.state import SessionDB
    from lohra.workflow.budget import Budget
    from lohra.workflow.engine import WorkflowEngine
    from lohra.workflow.schema import validate_spec
    from tests.test_workflow_max_iterations import _recording_core

    db = SessionDB(":memory:")
    core, _built = _recording_core(db, lambda prompt: "ok")
    try:
        spec = validate_spec(
            {
                "meta": {"name": "xp"},
                "nodes": [
                    {"id": "a", "type": "agent", "prompt": "go", "provider": "anthropic"}
                ],
            }
        )
        engine = WorkflowEngine(core, budget=Budget())  # client_pool=None
        result = engine.run(spec, {})
        assert result.outputs["a"] is None
        faults = "\n".join(result.faults)
        assert "a:" in faults and "provider unavailable" in faults
    finally:
        core.shutdown()
        db.close()
