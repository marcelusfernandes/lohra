"""Author-time LINT over an already-VALIDATED spec (issue #49).

``validate_spec`` (schema.py) has exactly two terminal outcomes: accept or
reject. A lint is neither — it flags a spec that is *valid* but suspicious, as
a WARNING that rides along with acceptance instead of blocking it. Reuses
``SpecIssue`` (schema.py) so the shape/rendering is one vocabulary; never
touches ``WorkflowSpec`` (frozen, no field to hang a warning on) or the
``ValidationError`` path.
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.graph import dependencies
from lohra.workflow.nodes import WorkflowSpec, iter_nested_entries
from lohra.workflow.schema import SpecIssue


def lint_spec(spec: WorkflowSpec) -> tuple[SpecIssue, ...]:
    """Didactic warnings over a spec ``validate_spec`` already accepted.

    Never raises, never rejects — the caller decides what to do with a
    non-empty result (surface it, log it; never turn it into an error)."""
    issues: list[SpecIssue] = []
    _lint_disconnected(spec, issues)
    _lint_nested_id_type(spec, issues)
    return tuple(issues)


def lint_warnings(spec: WorkflowSpec) -> list[dict[str, Any]]:
    """``lint_spec`` results, JSON-serializable — what a tool/event payload
    carries. Kept here (not in the caller) so a wire-up is a one-line call."""
    return [{"rule": i.rule, "message": i.message, "node_id": i.node_id} for i in lint_spec(spec)]


def with_warnings(base: dict[str, Any], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    """``base`` plus a ``warnings`` key ONLY when there are any — an empty
    list must never add the key, so a spec nothing was flagged on gets back
    the exact same reply shape it always did."""
    return {**base, "warnings": warnings} if warnings else base


def _lint_disconnected(spec: WorkflowSpec, issues: list[SpecIssue]) -> None:
    """Rule 1 (STRICT): more than one node and NOT ONE edge anywhere in the DAG.

    ``dependencies`` (graph.py) is the same function the engine's own
    topological order runs on: explicit ``depends_on`` UNION every ``${ref}``
    root. When every node comes back with an empty set, the N nodes have no
    relation to each other at all — the engine still runs them ONE AT A TIME,
    in the queue order it happened to pick, for no reason (engine.py's
    topological order has no concept of "independent, so parallel"). This is
    intentionally narrow: a spec where only SOME nodes are disconnected is a
    different (noisier) lint, not implemented here — a real pipeline often has
    one setup node feeding everything else and one truly standalone side node.
    """
    if len(spec.nodes) <= 1:
        return
    node_ids = {n.id for n in spec.nodes}
    if any(dependencies(node, node_ids) for node in spec.nodes):
        return
    issues.append(
        SpecIssue(
            "disconnected_dag",
            f"{len(spec.nodes)} nodes share no 'depends_on' or ${{ref}} anywhere "
            "in this spec — they still run ONE AT A TIME, in a queue, just with "
            "no relation between them. If they should run together, make them "
            "branches of a 'parallel' node; if one needs another's output (or "
            "just needs to run after it), add 'depends_on' or a ${ref}. See "
            "skill_view('workflow-authoring') for worked examples.",
        )
    )


def _lint_nested_id_type(spec: WorkflowSpec, issues: list[SpecIssue]) -> None:
    """Rule 2 (issue #82 follow-up, decisão do coordenador por delegação do
    dono, 2026-09-05): ``id``/
    ``type: "agent"`` on an embedded shape (a ``parallel`` branch, a
    ``judge_panel`` attempt/``synthesize``, a ``pipeline`` stage, a
    ``loop_until_dry``/``gate`` ``body``) mirror the top-level node mould but
    are never read anywhere — results are positional, never addressable by
    id, and the shape is always agent-like already. ``schema_nested.py``
    ACCEPTS both (refusing them broke real templates on upgrade for a field
    that never changed behaviour); this rule is the loud warning that keeps
    "no field silently ignored" true anyway. One warning per node, not per
    occurrence — a panel with ten attempts all carrying ``id`` needs saying
    once, not ten times."""
    for node in spec.nodes:
        if any("id" in entry or "type" in entry for _, _, entry in iter_nested_entries(node)):
            issues.append(
                SpecIssue(
                    "nested_id_type_ignored",
                    "this node has a branch/attempt/stage/body carrying 'id' "
                    "and/or 'type' — both are ignored: the result is collected "
                    "positionally (by index/order), never addressable by id, "
                    "and the shape is always agent-like already. Drop them; "
                    "they cost nothing today, but it reads as a guarantee ('this "
                    "branch is called x') that does not exist.",
                    node_id=node.id,
                )
            )
