"""Human answer addresses (#106), independent of ambiguous report labels.

The JSON type is the protocol discriminator: a list contains structured
addresses; an object is the legacy label map. No authored id can impersonate
the other protocol. Legacy matching examines ALL declared root calls before
execution, without loading templates or depending on which question ran first.
"""

from __future__ import annotations

import json
from typing import Any

from lohra.workflow.namespacing import sub_prefix
from lohra.workflow.nodes import WorkflowSpec

CheckpointInput = dict[str, Any] | list[dict[str, Any]] | None


def _nodes(spec: Any) -> list[dict]:
    if isinstance(spec, WorkflowSpec):
        return [{**node.fields, "id": node.id, "type": node.type} for node in spec.nodes]
    nodes = spec.get("nodes", []) if isinstance(spec, dict) else []
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def legacy_addresses(key: str, spec: Any, *, template: str | None = None) -> list[list[str]]:
    """Possible identities, conservatively including unloaded child templates.

    A matching call prefix is enough: looking inside the template to eliminate
    a candidate would depend on mutable/lazy external state. Only pending
    QUESTIONS may use their persisted template metadata to recover identity;
    old ANSWERS never get that privilege (it would guess the human's intent).
    """
    found = []
    for node in _nodes(spec):
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        if node.get("type") == "checkpoint" and key == node_id and template is None:
            found.append([node_id])
        if node.get("type") == "workflow" and (template is None or node.get("ref") == template):
            prefix = sub_prefix(node_id)
            if key.startswith(prefix) and key[len(prefix):]:
                found.append([node_id, key[len(prefix):]])
    return found


def valid_address(address: Any) -> bool:
    return (
        isinstance(address, list) and len(address) in (1, 2)
        and all(isinstance(part, str) and bool(part) for part in address)
    )


def answer_example(address: list[str]) -> str:
    return "checkpoint_answers: " + json.dumps(
        [{"address": address, "answer": "<human answer verbatim>"}], ensure_ascii=False,
    )


def normalize_answers(answers: CheckpointInput, spec: Any) -> list[dict[str, Any]]:
    """Copy into the structured protocol, or refuse before any answer is used."""
    if answers is None:
        return []
    if isinstance(answers, dict):
        entries = []
        for key, answer in answers.items():
            if not isinstance(key, str):
                raise ValueError("checkpoint_answers legacy keys must be strings")
            candidates = legacy_addresses(key, spec)
            if len(candidates) > 1:
                choices = "; OR ".join(answer_example(address) for address in candidates)
                raise ValueError(
                    f"ambiguous legacy checkpoint answer {key!r}; no answer was applied. "
                    f"Ask the HUMAN which question they answered and send only its address: {choices}"
                )
            if candidates:  # unmatched legacy keys have always been ignored
                entries.append({"address": candidates[0], "answer": answer})
        return entries
    if not isinstance(answers, list):
        raise ValueError("checkpoint_answers must be a legacy object or a list of {address, answer}")
    entries, seen = [], set()
    for entry in answers:
        if (not isinstance(entry, dict) or set(entry) != {"address", "answer"}
                or not valid_address(entry["address"])):
            raise ValueError(
                "checkpoint_answers entries require exactly address (one or two nonempty "
                "string ids) and answer (any JSON value, including null)"
            )
        address = tuple(entry["address"])
        if address in seen:
            raise ValueError(f"duplicate checkpoint answer address {list(address)!r}")
        seen.add(address)
        entries.append({"address": list(address), "answer": entry["answer"]})
    return entries


def answer_map(entries: list[dict[str, Any]]) -> dict[tuple[str, ...], Any]:
    return {tuple(entry["address"]): entry["answer"] for entry in entries}


def pending_address(pending: dict, spec: Any) -> list[str] | None:
    """Recover old questions using their recorded provenance, never their answer."""
    if "answer_address" in pending:
        address = pending["answer_address"]
        return list(address) if valid_address(address) else None
    key = pending.get("node_id")
    if not isinstance(key, str) or not key:
        return None
    if not pending.get("template"):
        # Pre-#78 children persisted a BARE node_id and no template. A root id
        # match therefore cannot prove who asked, even with one current call.
        # Never assign such a child's old default to a guarded root (#106).
        if any(node.get("type") == "workflow" for node in _nodes(spec)):
            return None
        roots = [address for address in legacy_addresses(key, spec) if len(address) == 1]
        return roots[0] if len(roots) == 1 else None
    addresses = legacy_addresses(key, spec, template=pending["template"])
    return addresses[0] if len(addresses) == 1 else None
