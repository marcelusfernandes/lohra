"""Typed references — pure path lookups, resolved STRICTLY single-pass.

Grammar discipline (spec §2.2): a reference is ``${path}`` where path is dotted
identifiers / integer indices ONLY — no expressions, arithmetic, calls, or
conditionals. The moment a reference grows expression syntax you have reinvented
code. ``find_refs``/``is_valid_ref`` police the authored spec at validation time.

Single-pass (spec §2.3): ``resolve_value`` substitutes ``${...}`` found in the
AUTHORED text once; resolved values are inserted as inert literals and NEVER
re-scanned — so untrusted leaf output containing ``${...}`` cannot inject a
second-order reference. (``re.sub`` does not re-scan its replacements.)
"""

from __future__ import annotations

import json
import re
from typing import Any

from lohra.workflow.jsonio import UNPARSEABLE, loads_lenient

_REF_RE = re.compile(r"\$\{([^}]*)\}")
# A path: an identifier or integer segment, dot-separated. Nothing else.
_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:[A-Za-z_][A-Za-z0-9_]*|\d+))*\Z")


def is_valid_ref(inner: str) -> bool:
    """Whether the text inside ``${...}`` is a pure path (not an expression)."""
    return bool(_PATH_RE.match(inner.strip())) and inner == inner.strip()


def find_refs(text: str) -> list[str]:
    """Every ``${...}`` inner found in ``text`` (in order)."""
    return _REF_RE.findall(text)


def invalid_refs(text: str) -> list[str]:
    """The inners that are expression-like (would reintroduce code)."""
    return [inner for inner in find_refs(text) if not is_valid_ref(inner)]


def _lookup(path: str, context: dict[str, Any]) -> Any:
    """Walk a dotted path through dicts/lists; None if any segment is missing.

    If a segment is requested on a STRING value, try to parse it leniently as
    JSON first — so ``${gen.claims}`` works even when ``gen`` returned its JSON as
    raw/fenced text (e.g. a node with no schema). A whole-value ``${gen}`` is
    unaffected: parsing only kicks in when descending into a field."""
    current: Any = context
    for segment in path.split("."):
        if isinstance(current, str):
            parsed = loads_lenient(current)
            if parsed is UNPARSEABLE:
                return None
            current = parsed
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, (list, tuple)) and segment.isdigit():
            index = int(segment)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def resolve_value(value: Any, context: dict[str, Any]) -> Any:
    """Resolve ``${...}`` references in an AUTHORED value, exactly once.

    - A whole-value ref (the string IS one ``${...}``) returns the looked-up
      object with its TYPE preserved (so ``items: ${scan.ids}`` stays a list).
    - An embedded ref substitutes the stringified value into the surrounding text.
    - Lists/dicts are walked (authored structure only).
    Substituted values are inert: never re-scanned for ``${...}``.
    """
    if isinstance(value, str):
        whole = _REF_RE.fullmatch(value.strip())
        if whole is not None:
            return _lookup(whole.group(1).strip(), context)
        # Embedded: one pass; re.sub does not re-scan replacements.
        return _REF_RE.sub(lambda m: _stringify(_lookup(m.group(1).strip(), context)), value)
    if isinstance(value, list):
        return [resolve_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: resolve_value(item, context) for key, item in value.items()}
    return value


def resolve_strict(value: Any, context: dict[str, Any]) -> tuple[Any, str | None]:
    """Like ``resolve_value``, but REFUSES to substitute an upstream null.

    Returns ``(resolved, None)`` or ``(None, path)`` naming the first reference
    that resolved to None. A leaf prompted with the literal "null" is worse than
    a node that fails: the model reads it as content and confabulates over the
    hole, so a null upstream must fail the node (fail-closed, §7.5).
    """
    if not isinstance(value, str):
        return resolve_value(value, context), None
    whole = _REF_RE.fullmatch(value.strip())
    if whole is not None:
        path = whole.group(1).strip()
        found = _lookup(path, context)
        return (found, None) if found is not None else (None, path)
    missing: list[str] = []

    def substitute(match: re.Match) -> str:
        path = match.group(1).strip()
        found = _lookup(path, context)
        if found is None:
            missing.append(path)
            return ""
        return _stringify(found)

    text = _REF_RE.sub(substitute, value)  # one pass; replacements are never re-scanned
    return (None, missing[0]) if missing else (text, None)


def first_aggregate_hole(
    value: Any, context: dict[str, Any], aggregate_types: dict[str, str]
) -> tuple[str, int] | None:
    """First ``(node_id, index)`` where a reference to a WHOLE aggregation output
    would carry a dead top-level element — or None.

    Scoped twice, on purpose (§7.5, issue #72). Only a BARE root is inspected:
    ``${p}`` IS the aggregation's output, while ``${p.0}`` names one branch, and
    refusing that because a SIBLING died would kill a node that reads nothing
    dead. And only the TOP level of it: a ``None`` deeper inside is a leaf's own
    answer (a nullable field of its schema), which the harness has no business
    calling a hole.
    """
    if not isinstance(value, str) or not aggregate_types:
        return None
    for inner in find_refs(value):
        path = inner.strip()
        if path not in aggregate_types or not is_valid_ref(inner):
            continue
        found = _lookup(path, context)
        if not isinstance(found, (list, tuple)):
            continue
        for index, item in enumerate(found):
            if item is None:
                return path, index
    return None
