"""Invocation-aware cell keys and conservative legacy reads (#90).

The old hash knows content, not which call produced it. A current spec cannot
reconstruct that history: calls can be removed or repointed between resumes.
Legacy artifact owners are the only row-local witness predating this column.
"""

from dataclasses import dataclass
import json
from typing import Any

from lohra.workflow.cache import content_hash
from lohra.workflow.namespacing import sub_prefix

LEGACY_SCOPE_UNPROVEN = "legacy_scope_unproven"
REVALIDATION = (
    "Legacy cached approval has no matching invocation provenance; answer this "
    "checkpoint again using the node_id shown here."
)


def scoped_node(scope: tuple[str, ...], node_id: str) -> str:
    return "".join(sub_prefix(part) for part in scope) + node_id


@dataclass(frozen=True)
class CellRead:
    hit: bool
    output: Any = None
    artifact: dict | None = None
    stamp: dict | None = None
    source_hash: str = ""
    reason: str | None = None


def _stored_scope(row: dict) -> tuple[str, ...] | None:
    try:
        value = json.loads(row.get("node_scope_json") or "null")
    except (TypeError, ValueError):
        return None
    return tuple(value) if isinstance(value, list) and all(isinstance(p, str) for p in value) else None


def _legacy_owner(row: dict, artifact: dict | None, scope: tuple[str, ...], node_id: str) -> bool:
    # No permissive parsing/fallback from RunPaths: every entry must carry the
    # harness's explicit owner, and the raw row id must match too. Together they
    # distinguish authored ids containing the namespace's punctuation.
    if row.get("node_id") != node_id or not artifact:
        return False
    entries = artifact.get("entries")
    expected = scoped_node(scope, node_id)
    return bool(entries) and isinstance(entries, list) and all(
        isinstance(entry, dict) and entry.get("owner") == expected for entry in entries
    )


class CellKeys:
    """Shared by the executing and preview engines; retains hashes, never prompts.

    Only nested keys change. Root checkpoint hits additionally need a recorded
    scope: their unchanged old key could otherwise read an old nested approval.
    """

    def __init__(self, scope: tuple[str, ...] = ()) -> None:
        self.scope = tuple(scope)
        self._keys: dict[str, tuple[str, bool]] = {}

    def hash(self, spec_id: tuple[Any, Any], *parts: Any) -> str:
        legacy = content_hash(*spec_id, *parts)
        key = content_hash("invocation-v1", self.scope, legacy) if self.scope else legacy
        self._keys[key] = (legacy, len(parts) > 1 and parts[1] == "checkpoint")
        return key

    def read(self, cache: Any, chash: str, node_id: str) -> CellRead:
        legacy, checkpoint = self._keys.get(chash, (chash, False))
        source = chash
        hit, output, artifact, row = cache.read_cell(source)
        if not hit and legacy != chash:
            source = legacy
            hit, output, artifact, row = cache.read_cell(source)
        if not hit or row is None:
            return CellRead(False)
        recorded_scope = _stored_scope(row)
        if source != chash and recorded_scope is not None and recorded_scope != self.scope:
            # A newly recorded root is not a legacy approval for this child.
            return CellRead(False)
        scoped = (
            row.get("node_id") == scoped_node(self.scope, node_id)
            and _stored_scope(row) == self.scope
        )
        # Human answers never went through artifact measurement. A fabricated
        # manifest cannot substitute for their explicit scope column.
        if checkpoint:
            proven = scoped
        elif self.scope:
            proven = scoped or (
                source == legacy and _legacy_owner(row, artifact, self.scope, node_id)
            )
        else:
            proven = True  # preserve legacy root leaf replay
        if not proven:
            return CellRead(False, source_hash=source, reason=LEGACY_SCOPE_UNPROVEN)
        stamp = {key: row.get(key) for key in ("policy_hash", "harness_version")}
        return CellRead(True, output, artifact, stamp, source)
