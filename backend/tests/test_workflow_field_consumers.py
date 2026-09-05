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

The one deliberate exception is `loop_until_dry.budget`: issue #71 is landing
the token-budget vocabulary this field will use, and the coordinator asked
this field be left alone until that contract exists (a follow-up to this same
issue). Its entry is the literal string `"pending #73 follow-up"` rather than
`NO_CONSUMER` — an honest, explicit debt marker, not a silent pass. See
docs/specs/07-workflow-harness.md for the field vocabulary.
"""

from __future__ import annotations

import pytest

from lohra.workflow.nodes import NODE_SPECS

# The sentinel: a field with genuinely NO reader anywhere in the package.
# A table entry equal to this fails the per-field test below.
NO_CONSUMER = "NO_CONSUMER — dead field, see issue #73"

# The explicit, honest debt marker for the one field deferred to a named
# follow-up (NOT the same as NO_CONSUMER — this is a documented exception,
# not a silent one).
PENDING_71 = "pending #73 follow-up (token-budget vocabulary lands with #71)"

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
        # Deliberately deferred, not NO_CONSUMER: issue #71 is landing the
        # token-budget vocabulary this field will use; the coordinator asked
        # this one field be left alone until that contract exists. The table
        # says so honestly rather than passing silently.
        budget=PENDING_71,
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
