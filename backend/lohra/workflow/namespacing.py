"""One spelling for invocation labels — ``sub[<call>]:`` (#90).

IDs, human answer keys and faults all name the calling workflow node. The
callee remains explicit ``template`` metadata in checkpoint/route payloads,
cache preview entries and costs. Content hashes use structured scope, never
these display strings; changing a label cannot conflate cache cells.
"""

from __future__ import annotations

from typing import Any


def sub_prefix(ref: Any) -> str:
    """The namespace one nesting level down, ``sub[<call>]:``."""
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
