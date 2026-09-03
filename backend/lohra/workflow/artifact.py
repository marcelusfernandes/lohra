"""Artifact manifests — what a leaf CLAIMS it wrote, measured by the harness (#45 E4).

A cell is the leaf's ``output_json``. It knows nothing about the filesystem, so
a node that "produced ``docs/report.md``" caches PROSE about a file, and a later
replay re-asserts that prose whatever the file says now. The investigation of
the real ``lohra-notion-v4`` run found exactly that: 3 of 5 artefacts declared by
cells were mutated AFTER their cell was written (by a live, legitimate leaf), and
two cells replayed twice asserting what was no longer true. The damage was zero
only because that spec had zero ``${ref}`` edges — a negative control, not a
guarantee.

The primitive this adds is the smallest honest one: a reserved output schema
(``artifact_manifest`` / ``artifact_manifests``) whose ``path`` the HARNESS then
measures itself. The leaf's own ``sha256``/``bytes`` are a CLAIM, kept as a hint
and never trusted — a divergence between the claim and the measurement is a
warning fault, not a dead node.

Two rules hold the surface honest:

1. **Scope.** The harness only ever stats/hashes a path inside the run's own
   tree (``runs/<run_id>/``) or an operator ``fs_allow`` root. Anything else —
   the project the leaf reached through an operator-enabled shell, ``/etc``, a
   relative path whose cwd is unknowable — is ``unverifiable`` and is NEVER
   opened. The v4 case (a leaf writing into the user's project via ``terminal``)
   is therefore reported as ``unverifiable``, which is the true answer.
2. **Lexical containment first.** ``Path.resolve()`` lstats every component, so
   the containment test is a pure string prefix check on the normalised absolute
   path — zero syscalls for a path we are not allowed to touch. Only a path that
   passes it is resolved (catching a symlink that escapes) and only then stat'd.

The measurement never enters ``output_json``: it rides in sidecar columns of
``workflow_node_cache`` and flows to nobody's ``${ref}``. What a downstream node
reads is what the leaf said, exactly as before.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The reserved schema names. An author references them with ``schema_ref`` and
# defines NOTHING; the validator refuses a spec that tries to redefine them, so
# the name always means the shape the harness knows how to measure.
ARTIFACT_MANIFEST = "artifact_manifest"
ARTIFACT_MANIFESTS = "artifact_manifests"

# ``sha256``/``bytes`` are deliberately NOT pattern-constrained: they are the
# leaf's claim (a hint the harness cross-checks), and a schema retry burnt on a
# malformed hex digest would buy nothing — the harness measures the file anyway.
MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "sha256": {"type": "string"},
        "bytes": {"type": "integer"},
    },
    "required": ["path"],
}
MANIFESTS_SCHEMA: dict[str, Any] = {"type": "array", "items": MANIFEST_SCHEMA, "minItems": 1}

BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    ARTIFACT_MANIFEST: MANIFEST_SCHEMA,
    ARTIFACT_MANIFESTS: MANIFESTS_SCHEMA,
}
RESERVED_SCHEMA_NAMES = frozenset(BUILTIN_SCHEMAS)

# Cell-level verification verdicts, stored next to the cell.
VERIFIED = "verified"          # at least one declared path was measured
MISSING = "missing"            # in scope, and not there
UNVERIFIABLE = "unverifiable"  # out of scope / unreadable — never opened
CHANGED = "changed"            # only a RECHECK produces this one

# A manifest is a handoff, not a payload dump: 32 entries is far past any real
# node and keeps a hostile answer from turning a cache store into a filesystem
# sweep. Extra entries are dropped from the MEASUREMENT only — output_json,
# which is what downstream reads, keeps every one of them.
MAX_ENTRIES = 32
# Hashing is bounded like every other read in the harness. A file past the cap
# is unverifiable rather than a stall; the size still comes from stat.
MAX_HASH_BYTES = 64 * 1024 * 1024
_CHUNK = 1024 * 1024


def is_manifest_schema(schema: Any) -> bool:
    """True when this resolved schema IS one of the reserved manifest shapes.

    Identity first (``resolve_schema`` hands back the module-level dict), value
    equality second — an author who inlines the same shape declared the same
    thing and gets the same measurement."""
    if not isinstance(schema, dict):
        return False
    return any(schema is known or schema == known for known in BUILTIN_SCHEMAS.values())


@dataclass(frozen=True)
class ArtifactScope:
    """The absolute roots the harness may stat and hash inside — nothing else.

    NOT the leaf sandbox (that one is per-acquisition, ``work-{fence}``): a cell
    stored under ``work-3`` has to stay verifiable when the resume owns
    ``work-4``, so the scope is the run's WHOLE tree plus the operator's
    ``fs_allow`` roots. It only ever widens what may be READ for measurement,
    and every one of those roots is already readable by this process."""

    roots: tuple[str, ...] = ()

    @classmethod
    def of(cls, run_root: Any = None, policy: Any = None) -> "ArtifactScope":
        """``runs/<run_id>/`` + every ``fs_allow`` root (ro and rw alike — the
        harness only ever reads)."""
        raw: list[Any] = []
        if run_root is not None:
            raw.append(run_root)
        for entry in getattr(policy, "fs_allow", ()) or ():
            raw.append(getattr(entry, "path", entry))
        roots = tuple(dict.fromkeys(p for p in (_absolute(item) for item in raw) if p))
        return cls(roots)

    def contains(self, absolute: str) -> bool:
        """Pure string containment — no syscall, so a denied path is never
        touched (not even the lstat ``Path.resolve`` would do per component)."""
        for root in self.roots:
            if absolute == root or absolute.startswith(root.rstrip(os.sep) + os.sep):
                return True
        return False


def _absolute(raw: Any) -> str | None:
    """A normalised absolute path string, or None for anything unusable.

    A RELATIVE path is None on purpose: a leaf's cwd is not knowable from here,
    so resolving it against ours would measure a file nobody named."""
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    expanded = os.path.expanduser(raw.strip())
    if not os.path.isabs(expanded):
        return None
    return os.path.normpath(expanded)


@dataclass(frozen=True)
class ArtifactRecord:
    """What the harness measured for one cell, and what it wants to complain about."""

    verification: str
    entries: tuple[dict[str, Any], ...]
    divergences: tuple[str, ...] = ()

    def as_entry_list(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.entries]


def claimed_entries(output: Any) -> list[dict[str, Any]]:
    """The manifest entries in a leaf's (already schema-validated) output.

    Tolerant on purpose: this also runs against rows written by an older Lohra
    and against an inline schema an author wrote by hand, so a shape that is not
    a manifest yields nothing rather than raising inside a cache store."""
    if isinstance(output, dict):
        return [output] if isinstance(output.get("path"), str) else []
    if isinstance(output, list):
        return [item for item in output if isinstance(item, dict) and isinstance(item.get("path"), str)]
    return []


def _hash_file(path: str) -> tuple[str | None, int]:
    """(sha256, bytes) of a regular file, or (None, size) when it cannot be
    hashed within the cap. Never follows a directory or a device."""
    digest = hashlib.sha256()
    read = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if read > MAX_HASH_BYTES:
                return None, read
            digest.update(chunk)
    return digest.hexdigest(), read


def measure(raw_path: Any, scope: ArtifactScope) -> dict[str, Any]:
    """Measure ONE declared path. Order is load-bearing (see the module docstring):
    lexical containment (no syscall) → resolve → containment again → stat → hash."""
    absolute = _absolute(raw_path)
    if absolute is None or not scope.contains(absolute):
        return {"path": absolute or str(raw_path), "status": UNVERIFIABLE}
    try:
        real = os.path.realpath(absolute)
        if not scope.contains(real):
            # A symlink pointing out of the scope: the target is somebody else's
            # file, and following it would be exactly the read this refuses.
            return {"path": absolute, "status": UNVERIFIABLE}
        info = os.stat(real)
    except FileNotFoundError:
        return {"path": absolute, "status": MISSING}
    except OSError:
        return {"path": absolute, "status": UNVERIFIABLE}
    if not os.path.isfile(real):
        return {"path": absolute, "status": UNVERIFIABLE}
    try:
        digest, size = _hash_file(real)
    except OSError:
        return {"path": absolute, "status": UNVERIFIABLE}
    if digest is None:
        return {"path": absolute, "status": UNVERIFIABLE, "bytes": int(info.st_size)}
    return {"path": absolute, "status": VERIFIED, "sha256": digest, "bytes": size}


def _verdict(entries: list[dict[str, Any]]) -> str:
    """The cell-level verdict. ``verified`` the moment ONE path was measured —
    that is the one a recheck can compare — then ``missing`` over
    ``unverifiable``, because "it is not there" is a stronger fact than "we may
    not look"."""
    statuses = {entry["status"] for entry in entries}
    if VERIFIED in statuses:
        return VERIFIED
    if MISSING in statuses:
        return MISSING
    return UNVERIFIABLE


def _divergence(claim: dict[str, Any], measured: dict[str, Any]) -> str | None:
    """The leaf's claim vs. the harness's measurement, for a MEASURED entry."""
    path = measured["path"]
    claimed_sha = claim.get("sha256")
    if isinstance(claimed_sha, str) and claimed_sha.strip().lower() != measured["sha256"]:
        return (
            f"artifact {path}: the leaf claimed sha256 {claimed_sha.strip()[:16]}… "
            f"but the harness measured {measured['sha256'][:16]}… (claim not trusted)"
        )
    claimed_bytes = claim.get("bytes")
    if isinstance(claimed_bytes, int) and not isinstance(claimed_bytes, bool):
        if claimed_bytes != measured["bytes"]:
            return (
                f"artifact {path}: the leaf claimed {claimed_bytes} bytes but the "
                f"harness measured {measured['bytes']} (claim not trusted)"
            )
    return None


def verify_output(output: Any, scope: ArtifactScope | None) -> ArtifactRecord | None:
    """Measure every path a manifest declares. None when it declares none.

    The returned record is what gets stored NEXT TO the cell — never inside it.
    ``divergences`` are warnings for the caller to record as faults: a leaf that
    lied about a hash still wrote a file, and killing the node over the lie would
    throw away work the harness can describe correctly."""
    claims = claimed_entries(output)
    if not claims:
        return None
    scope = scope if scope is not None else ArtifactScope()
    entries: list[dict[str, Any]] = []
    divergences: list[str] = []
    for claim in claims[:MAX_ENTRIES]:
        measured = measure(claim.get("path"), scope)
        entries.append(measured)
        if measured["status"] == VERIFIED:
            note = _divergence(claim, measured)
            if note is not None:
                divergences.append(note)
    if len(claims) > MAX_ENTRIES:
        divergences.append(
            f"artifact manifest declares {len(claims)} paths; only the first "
            f"{MAX_ENTRIES} were measured"
        )
    return ArtifactRecord(_verdict(entries), tuple(entries), tuple(divergences))


@dataclass(frozen=True)
class Recheck:
    """Whether a stored manifest still describes the filesystem.

    ``stale`` is the only field the engine acts on; ``status`` is what the audit
    event says happened (``verified`` / ``changed`` / ``missing`` /
    ``unverifiable``)."""

    stale: bool
    status: str


def recheck(entries: Any, scope: ArtifactScope | None) -> Recheck:
    """Re-measure a stored manifest. A cell whose file MOVED ON is not
    replayable: replaying it would re-assert a description of content that no
    longer exists, which is the lie this whole module exists to refuse.

    Only entries the harness itself measured are compared. One that has drifted
    out of scope (an ``fs_allow`` root the operator withdrew) is skipped rather
    than counted as changed: "we may not look" has never been evidence."""
    if not isinstance(entries, list):
        return Recheck(False, UNVERIFIABLE)
    scope = scope if scope is not None else ArtifactScope()
    compared = 0
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != VERIFIED:
            continue
        stored_sha = entry.get("sha256")
        if not isinstance(stored_sha, str):
            continue
        now = measure(entry.get("path"), scope)
        if now["status"] == MISSING:
            return Recheck(True, MISSING)
        if now["status"] != VERIFIED:
            continue  # out of scope now, or unreadable: not evidence of change
        compared += 1
        if now["sha256"] != stored_sha:
            return Recheck(True, CHANGED)
    return Recheck(False, VERIFIED if compared else UNVERIFIABLE)
