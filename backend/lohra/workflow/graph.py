"""Execution ORDER of a validated DAG — dependencies in, node list out.

Pure graph work over the spec: no engine, no core, no leaves. It lived in
``engine`` and ``events`` reached across for it by its private name; here both
read the same public one.
"""

from __future__ import annotations

from typing import Any

from lohra.workflow import refs
from lohra.workflow.nodes import Node, WorkflowSpec


def dependencies(node: Node, node_ids: set[str]) -> set[str]:
    """Node ids this node depends on (explicit depends_on + referenced nodes)."""
    deps: set[str] = set()
    explicit = node.fields.get("depends_on") or []
    if isinstance(explicit, list):
        deps |= {d for d in explicit if isinstance(d, str) and d in node_ids}
    deps |= {root for root in ref_roots(node.fields) if root in node_ids}
    return deps


def ref_roots(value: Any) -> set[str]:
    roots: set[str] = set()
    if isinstance(value, str):
        roots |= {inner.split(".")[0] for inner in refs.find_refs(value) if refs.is_valid_ref(inner)}
    elif isinstance(value, list):
        for item in value:
            roots |= ref_roots(item)
    elif isinstance(value, dict):
        for item in value.values():
            roots |= ref_roots(item)
    return roots


def topological_order(spec: WorkflowSpec) -> list[Node]:
    """Kahn's algorithm. The spec is already validated acyclic, so this resolves."""
    ids = {n.id for n in spec.nodes}
    by_id = {n.id: n for n in spec.nodes}
    pending = {n.id: dependencies(n, ids) for n in spec.nodes}
    ordered: list[Node] = []
    while pending:
        ready = [nid for nid, deps in pending.items() if deps <= {o.id for o in ordered}]
        if not ready:  # defensive — validation rejects cycles, so this shouldn't happen
            ordered.extend(by_id[nid] for nid in pending)
            break
        for nid in sorted(ready, key=lambda x: [n.id for n in spec.nodes].index(x)):
            ordered.append(by_id[nid])
            del pending[nid]
    return ordered

