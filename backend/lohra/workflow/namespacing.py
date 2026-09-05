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

And two CONTENTS inside that one shape, which is the subtle part:

- what a ROLLUP reports (faults, costs, route faults, ``required_failure``) is
  namespaced by the TEMPLATE — ``sub[<ref>]:`` — because a reader diagnosing a
  fault wants to know which template misbehaved. Externally documented, and
  unchanged since #61.
- what a HUMAN ANSWERS (``checkpoint_key``) is namespaced by the parent's
  ``workflow`` NODE — ``sub[<node id>]:``. The adversarial review of #78's first
  cut showed why it cannot be the ref: two nodes may run one template with
  different args ("delete staging?" and "delete PROD?"), which is two questions
  a person has to answer separately, and a ref-keyed answer opened both. Node
  ids are unique inside a spec by validation; template refs are not unique
  inside a spec at all.
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


def checkpoint_key(nested_node: Any | None, node_id: Any) -> str:
    """The key a human answers this checkpoint under (issue #78).

    ``nested_node`` is the PARENT's ``workflow`` node — the call, not the
    template it calls (see the module docstring). Namespaced one level down,
    bare at the top: an answer for a parent's gate can never reach a template's
    gate of the same id, an answer for one call can never open another's, and an
    answer for the template's can never open the parent's.
    ``MAX_WORKFLOW_DEPTH`` is 1, so one prefix level is the whole ladder — a
    deeper harness would compose these the way ``cache_preview`` already
    composes its own prefix.
    """
    return sub_node_id(nested_node, node_id) if nested_node else str(node_id)
