"""One spelling for invocation labels — ``sub[<call>]:`` (#90).

Display IDs, legacy answer aliases and faults name the calling workflow node. The
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


def checkpoint_label(nested_node: Any | None, node_id: Any) -> str:
    """Legacy display spelling, NOT an unambiguous answer address (#106).

    A root id may contain this same punctuation. Human answers use structured
    addresses; old label maps are resolved conservatively before execution.
    """
    return sub_node_id(nested_node, node_id) if nested_node is not None else str(node_id)
