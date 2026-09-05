"""What `required: true` MEANS when the node resolves to null (issue #15).

Spec 07 §7.4 has always promised it — "the run fails loudly (terminal
`status="failed"`, reason logged into rollup)" — and §7.5 spells out the same
policy for an engine fault on a required node ("abort the run"). Until now the
field was accepted by the validator and read by nobody, which is the one thing
a schema must never do: suggest an operational guarantee the runtime does not
apply.

The semantics, deliberately narrow:

- **opt-in.** Default `false` = the permissive fail-isolation the harness has
  always had (dead node -> `null`, run continues, `${ref}` stays fail-closed).
  Nothing about an existing spec changes.
- **the run stops there.** Not "the dependents are nulled": nulling what never
  ran would poison ``null_rate`` and read downstream as "the leaves died" —
  exactly the reasoning the cancel/pause paths already follow.
- **every node that did not run says why**, and says it honestly: a node that
  really depended on the failure (explicit ``depends_on`` or a ``${ref}``) is
  told so; a node that merely came later in the schedule is told the run was
  aborted. Blaming an edge that does not exist would send the author hunting
  for it.

Pure graph + string work: no engine, no core, no leaves — so the messages an
author will read are testable on their own.
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.graph import dependencies
from lohra.workflow.nodes import Node, WorkflowSpec


def required_fault(node_id: str) -> str:
    """The fault the failing node itself gets."""
    return (
        f"{node_id}: required node resolved to null — run aborted "
        f"(required: true; the remaining nodes were not scheduled)"
    )


def completeness_gaps(node: Node, output: Any) -> list | None:
    """The gaps a ``completeness_check`` reported, or None if it reported none.

    Issue #74. ``required`` has always meant one thing — "the node produced
    nothing" — and a completeness critic never produces nothing: it answers the
    fixed ``{complete, missing}``, and ``{"complete": false}`` is a well-formed
    answer that says the work is NOT done. Reading that as a success is what let
    a ``required`` audit certify an incomplete run.

    Deliberately narrow: only this node type, only an EXPLICIT ``false`` (a
    missing or unreadable ``complete`` is not a claim of incompleteness), and
    only ever consulted when the author wrote ``required: true``."""
    if node.type != "completeness_check" or not isinstance(output, dict):
        return None
    if output.get("complete") is not False:
        return None
    missing = output.get("missing")
    return list(missing) if isinstance(missing, list) else []


def completeness_fault(node_id: str, missing: list) -> str:
    """...and what the author reads when that ends the run.

    The first three gaps, verbatim: faults are prose an agent relays, and a
    critic that found forty gaps would bury the rollup. The full list survives
    in ``outputs`` — the dict is PRESERVED (never nulled) precisely so the next
    stretch can work from it."""
    head = f"{node_id}: completeness check found gaps: {missing[:3]}"
    if len(missing) > 3:
        head = f"{head} (+{len(missing) - 3} more)"
    # The remedy, said out loud: the verdict is a CELL like any other, so a bare
    # resume replays it and fails again. Without this line the honest next step
    # ("close the gaps, then change what the audit reads") reads as "retry".
    return (
        f"{head} — run aborted (required: true); the verdict is cached — change "
        f"the spec or args to re-check"
    )


def nested_required_fault(node_id: str, failing: str) -> str:
    """...and the one a `workflow` node gets when the abort happened INSIDE it.

    ``failing`` is already namespaced by ``fold_nested`` (``sub[ref]:inner``),
    so the parent's rollup can match it back to the sub-workflow that raised it.
    """
    return (
        f"{node_id}: required node {failing!r} failed inside the nested workflow "
        f"— run aborted"
    )


def dependents_of(spec: WorkflowSpec, node_id: str) -> set[str]:
    """Every node that transitively depends on ``node_id``.

    "Depends" is the engine's own definition (``graph.dependencies``): explicit
    ``depends_on`` UNION the roots of every ``${ref}`` the node reads. Anything
    else would let the fault text disagree with the order the run really took.
    """
    ids = {node.id for node in spec.nodes}
    edges = {node.id: dependencies(node, ids) for node in spec.nodes}
    found: set[str] = set()
    changed = True
    while changed:  # bounded: each pass either adds a node or stops
        changed = False
        for nid, deps in edges.items():
            if nid == node_id or nid in found:
                continue
            if node_id in deps or deps & found:
                found.add(nid)
                changed = True
    return found


def skip_fault(spec: WorkflowSpec, failed_id: str, node: Node) -> str:
    """The fault for ONE node that never got to run."""
    return skip_faults(spec, failed_id, [node])[0]


def skip_faults(spec: WorkflowSpec, failed_id: str, nodes: list[Node]) -> list[str]:
    """One honest fault per node the abort skipped, in schedule order."""
    dependents = dependents_of(spec, failed_id)
    return [
        f"{node.id}: skipped: required upstream {failed_id!r} failed"
        if node.id in dependents
        else f"{node.id}: skipped: run aborted by failed required node {failed_id!r}"
        for node in nodes
    ]
