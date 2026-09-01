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

from lohra.workflow.graph import dependencies
from lohra.workflow.nodes import Node, WorkflowSpec


def required_fault(node_id: str) -> str:
    """The fault the failing node itself gets."""
    return (
        f"{node_id}: required node resolved to null — run aborted "
        f"(required: true; the remaining nodes were not scheduled)"
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
