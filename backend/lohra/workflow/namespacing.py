"""One spelling of the nested namespace — ``sub[<ref>]:``.

A nested ``workflow`` node's children keep their own ids: two templates (and a
template and its parent) may both call a node ``cp`` without knowing about each
other. Everything the parent reports about them therefore carries the template
it came from, and it has done so since ``fold_nested`` first folded a nested
run's faults and per-node costs into the parent's rollup.

The prefix was spelled inline at every one of those sites. It is here now
because issue #78 added the site that MATTERS: the key a human's
``checkpoint_answers`` reaches a nested gate under. A second spelling there —
one character of drift — would silently hand the parent's approval to the
child's "delete prod?" gate, which is the whole bug. One function, one string.

Two shapes, because the two readers are different:

- an IDENTITY (``sub[child]:cp``) is a key: a node id in a rollup, a cost row,
  a pause payload, an answer mapping. No space — it is looked up, not read.
- a FAULT (``sub[child]: cp: …``) is prose a human or an agent relays, and the
  space is what keeps the nested sentence legible after the prefix.
"""

from __future__ import annotations

from typing import Any


def sub_prefix(ref: Any) -> str:
    """The namespace one nesting level down, ``sub[<ref>]:``."""
    return f"sub[{ref}]:"


def sub_node_id(ref: Any, node_id: Any) -> str:
    """A nested node's identity as the parent reports it."""
    return f"{sub_prefix(ref)}{node_id}"


def sub_fault(ref: Any, message: Any) -> str:
    """A nested fault as the parent reports it — prose, so it keeps the space."""
    return f"{sub_prefix(ref)} {message}"


def checkpoint_key(nested_ref: Any | None, node_id: Any) -> str:
    """The key a human answers this checkpoint under (issue #78).

    Namespaced one level down, bare at the top: an answer for a parent's gate
    can never reach a template's gate of the same id, and an answer for the
    template's can never open the parent's. ``MAX_WORKFLOW_DEPTH`` is 1, so one
    prefix level is the whole ladder — a deeper harness would compose these the
    way ``cache_preview`` already composes its own prefix.
    """
    return sub_node_id(nested_ref, node_id) if nested_ref else str(node_id)
