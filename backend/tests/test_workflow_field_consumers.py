"""Anti-drift test (issue #73): every field `NODE_SPECS` accepts a node must
have a real CONSUMER somewhere in the `lohra.workflow` package — a place that
actually reads `node.fields[...]` (or a dataclass property backing it) and
does something with the value. A field the validator accepts and nothing ever
reads is "accepted-and-ignored" — the exact defect issue #73 found for
`label`, `phase` and `Budget.pool_width` (three fields the skill had to admit
in writing "still validate but do nothing").

The table below (`FIELD_CONSUMERS`) is the CONTRACT: {node_type: {field:
pointer}}. It is asserted STRUCTURALLY against `NODE_SPECS` (same node types,
same field names per type) so a future field added to `nodes.py` without a
matching table entry fails the structural test below — the table cannot
silently fall out of sync with the registry it documents. Every listed pointer
must not be the `NO_CONSUMER` sentinel, which marks a field with no reader at
all.

`loop_until_dry.budget` was the one deliberate, named exception while this
table was first built — issue #71 was landing the token-budget vocabulary it
needed. It landed, and the field has a real consumer now
(`strategies.run_loop_until_dry`, `test_workflow_loop_budget.py`); see
docs/specs/07-workflow-harness.md §2.5 for the field vocabulary.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from lohra.workflow.nodes import NODE_SPECS

# The sentinel: a field with genuinely NO reader anywhere in the package.
# A table entry equal to this fails the per-field test below.
NO_CONSUMER = "NO_CONSUMER — dead field, see issue #73"

# Fields shared by every node type (nodes.py's `_COMMON`). `label` and `phase`
# — the two dead fields issue #73 found — are GONE from `_COMMON` entirely:
# schema.py now refuses both didactically (see test_workflow_schema.py), so
# they are no longer FieldSpecs at all and have no entry here.
_COMMON_CONSUMERS = {
    "required": "nodes.Node.required, read by engine.py (a null output on a "
    "required node aborts the run)",
    "depends_on": "graph.dependencies (explicit edges, folded into execution order)",
}

# Fields shared by every rigor/routing node (nodes.py's `_ROUTING`).
_ROUTING_CONSUMERS = {
    "model": "strategies._resolve_routing",
    "tier": "strategies._resolve_routing",
    "effort": "strategies._resolve_routing",
    "provider": "strategies._resolve_routing",
}


def _node(**fields: str) -> dict[str, str]:
    return {**_COMMON_CONSUMERS, **fields}


def _routed_node(**fields: str) -> dict[str, str]:
    return {**_COMMON_CONSUMERS, **_ROUTING_CONSUMERS, **fields}


FIELD_CONSUMERS: dict[str, dict[str, str]] = {
    "agent": _routed_node(
        prompt="strategies.run_agent (strict_prompt)",
        schema="nodes.resolve_schema, via engine.resolve_schema",
        schema_ref="nodes.resolve_schema, via engine.resolve_schema",
        tool_less="strategies._node_configure (forces a synthetic structured tool)",
        timeout="nodes.node_timeout, read in strategies.run_agent's cell hash",
        retries="nodes.node_retries; leaf_retry.py; route_fault.py",
        max_iterations="nodes.node_max_iterations, applied in strategies._node_configure",
    ),
    "parallel": _node(
        branches="strategies.run_parallel (_leaf_prompts)",
    ),
    "pipeline": _node(
        items="strategies.run_pipeline; cache_preview.py",
        stages="strategies.run_pipeline; cache_preview.py",
    ),
    "loop_until_dry": _routed_node(
        body="strategies.run_loop_until_dry",
        stop_after_k_empty="strategies.run_loop_until_dry",
        max_rounds="strategies.run_loop_until_dry",
        # issue #73 follow-up (landed after #71): the node's own token
        # ceiling, checked between rounds — see strategies.run_loop_until_dry
        # and test_workflow_loop_budget.py.
        budget="strategies.run_loop_until_dry",
    ),
    "verify": _routed_node(
        finding="strategies.run_verify",
        skeptics="strategies.run_verify",
        lenses="strategies.run_verify",
        kill_if_majority_refute="strategies.run_verify",
    ),
    "judge_panel": _routed_node(
        attempts="strategies.run_judge_panel (_leaf_prompts)",
        judges="strategies.run_judge_panel",
        synthesize="strategies.run_judge_panel",
    ),
    "workflow": _node(
        ref="strategies.run_workflow",
        args="strategies.run_workflow",
    ),
    "gate": _routed_node(
        body="gates.run_gate",
        validator="gates.run_gate",
        attempts="nodes.gate_attempts, read by gates.run_gate",
    ),
    "completeness_check": _routed_node(
        task="gates.run_completeness_check",
        results="gates.run_completeness_check",
    ),
    "checkpoint": _node(
        prompt="gates.run_checkpoint",
        default="gates.run_checkpoint",
        accept="gates.run_checkpoint; nodes.checkpoint_accepts (issue #74)",
        on_reject="nodes.checkpoint_on_reject, read by gates.run_checkpoint (issue #74)",
    ),
}


def test_field_consumers_table_covers_exactly_the_registered_node_types():
    assert set(FIELD_CONSUMERS) == set(NODE_SPECS), (
        "FIELD_CONSUMERS and NODE_SPECS disagree on the node-type set — a node "
        "type was added to (or removed from) nodes.py without updating this "
        "anti-drift table (issue #73)."
    )


@pytest.mark.parametrize("node_type", sorted(NODE_SPECS))
def test_field_consumers_table_covers_exactly_the_declared_fields(node_type):
    declared = NODE_SPECS[node_type].field_names()
    tabled = set(FIELD_CONSUMERS.get(node_type, {}))
    assert tabled == declared, (
        f"{node_type!r}: NODE_SPECS declares {sorted(declared)} but the "
        f"anti-drift table lists {sorted(tabled)}. A field was added to (or "
        "removed from) nodes.py without a matching FIELD_CONSUMERS entry — "
        "every accepted field needs a named consumer (issue #73)."
    )


def _flattened_fields():
    for node_type, fields in FIELD_CONSUMERS.items():
        for field_name, consumer in fields.items():
            yield pytest.param(node_type, field_name, consumer, id=f"{node_type}.{field_name}")


@pytest.mark.parametrize("node_type,field_name,consumer", list(_flattened_fields()))
def test_every_declared_field_has_a_real_consumer(node_type, field_name, consumer):
    """The core anti-drift assertion: no field may be `NO_CONSUMER`. A field
    that validates but is never read (issue #73's `label`/`phase`/dead
    `budget`) must fail HERE, loudly, instead of quietly shipping."""
    assert consumer != NO_CONSUMER, (
        f"{node_type}.{field_name} has no consumer anywhere in lohra.workflow "
        "— it validates but does nothing (issue #73). Either implement a "
        "reader, remove the field with a didactic schema.py refusal (the "
        "min_success_ratio mould), or — if there is a NAMED follow-up issue "
        "that will implement it — mark it with an honest pending-string, not "
        "NO_CONSUMER."
    )


def _first_token(segment: str) -> str:
    """The leading ``module.symbol`` word of one ``;``-separated pointer
    segment, stripped of trailing prose punctuation (a comma before "via ...",
    a stray "(")."""
    words = segment.strip().split()
    if not words:
        return ""
    return words[0].strip(",;:()")


def _pointer_resolves(token: str) -> bool:
    """Is ``token`` a real ``lohra.workflow.<module>[.<attr>...]`` path?

    A ``.py`` file reference (a test file named as extra context, e.g.
    ``test_workflow_loop_budget.py``) is not importable and is skipped — it is
    prose for a human, not a pointer this check can follow."""
    if not token or token.endswith(".py"):
        return False
    parts = token.split(".")
    if len(parts) < 2:
        return False
    module_name, *attrs = parts
    try:
        module = importlib.import_module(f"lohra.workflow.{module_name}")
    except ImportError:
        return False
    obj: Any = module
    for attr in attrs:
        try:
            obj = getattr(obj, attr)
        except AttributeError:
            return False
    return True


@pytest.mark.parametrize("node_type,field_name,consumer", list(_flattened_fields()))
def test_every_declared_field_consumer_pointer_resolves(node_type, field_name, consumer):
    """LOW-1 (adversarial review of #73): the table above is prose a human
    reviews, not a link the interpreter checks — a stale pointer (the real
    reader renamed or deleted, the table left alone) still reads as "not
    NO_CONSUMER" and would pass the test above. This makes at least one
    ``module.symbol`` token per entry resolve to a REAL import + attribute, so
    a rename without a matching table update fails loudly instead of only on
    manual review."""
    segments = consumer.split(";")
    tokens = [_first_token(seg) for seg in segments]
    assert any(_pointer_resolves(tok) for tok in tokens), (
        f"{node_type}.{field_name}: none of {tokens!r} resolves to a real "
        "lohra.workflow.<module>.<attr> — the pointer text drifted from the "
        "code it claims to point at (issue #73 LOW-1)."
    )
