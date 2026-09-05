"""What a cached cell RAN UNDER — operator policy + harness version (#75).

The cell key (``strategies.py``) is the cell's CONTENT: prompt, schema, route,
timeout, retries. It deliberately does not include the operator's sandbox policy
(which lives outside the spec by design, ``sandbox.py``) nor the version of the
harness that executed it. So a run that pauses under ``allow_terminal: true``,
is narrowed by its operator, and then resumes, replays the old cell as if
nothing had changed — and the audit of that run cannot say under which policy
each cell ran.

The owner's decision (issue #75, option B) is to MARK, never to invalidate:

- the cell stores the two facts as METADATA (``policy_hash``,
  ``harness_version``), in the cell's own guarded transaction;
- on a hit, a divergence is an ADVISORY fault (the #45 precedent: the node
  CONCLUDED, so the run's verdict is not what is wrong) plus a ``reason`` on the
  ``cache.replayed`` event;
- nothing is recomputed. A completed cell is work the run already paid for; the
  operator restricted the FUTURE, not the past.

Invariant: a stored NULL means "unknown", never "different". Every cell written
before this shipped reads as NULL and replays exactly as it always did — the
harness must not invent a divergence where it has no record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lohra.workflow.cache import content_hash

# WHY a replay is worth a mention, decided at the lookup. Identifiers, never
# prose: they ride into the durable audit trail, whose ``reason`` vocabulary is
# a closed allow-list (``audit.py``).
REASON_POLICY_CHANGED = "policy_changed"
REASON_HARNESS_VERSION_CHANGED = "harness_version_changed"
REASON_POLICY_AND_HARNESS_VERSION_CHANGED = "policy_and_harness_version_changed"

# The half of the message that says why this is an advice and not a verdict —
# the same shape the artifact advisory uses (``artifact.py``).
_ADVISORY_TAIL = (
    " (advisory: nothing was recomputed — the cell is work the run already paid "
    "for; what changed is what the leaf WOULD do today)"
)


def harness_version() -> str:
    """The running harness's version, read at CALL time.

    Deliberately not bound at import: two stretches of one run can be two
    different installs, and a test that pins the upgrade has to be able to say
    so without reloading the package."""
    from lohra import __version__

    return __version__


def policy_fingerprint(policy: Any) -> str:
    """Canonical sha256 of the EFFECTIVE leaf capability policy.

    All four gates ``sandbox_dispatch`` applies, in a canonical (SORTED) shape:
    reordering ``workflow_policy.json`` is not a policy change, and a
    fingerprint that said otherwise would fault every replay of a run whose
    operator merely tidied the file.

    ``egress_allow`` is in here alongside the three the issue names: it is one
    of the four capability classes the sandbox gates, so leaving it out would
    make "same policy" a claim the harness cannot support.

    Paths are compared as WRITTEN (expanded, not resolved): resolving would take
    a syscall per lookup and would call a root that moved underneath a symlink a
    policy change, which is a different fact from the operator editing the
    policy."""
    return content_hash(
        {
            "allow_terminal": bool(getattr(policy, "allow_terminal", False)),
            "egress_allow": sorted(
                str(host).lower() for host in getattr(policy, "egress_allow", ())
            ),
            "fs_allow": sorted(
                [str(root.path), bool(root.writable)]
                for root in getattr(policy, "fs_allow", ())
            ),
            "mcp_allow": sorted(str(server) for server in getattr(policy, "mcp_allow", ())),
        }
    )


@dataclass(frozen=True)
class CellStamp:
    """Under what a cell ran. ``None`` on either field means UNKNOWN."""

    policy_hash: str | None = None
    harness_version: str | None = None

    @classmethod
    def current(cls, policy: Any) -> "CellStamp":
        """The stamp a cell stored RIGHT NOW would carry."""
        return cls(policy_fingerprint(policy), harness_version())

    @classmethod
    def stored(cls, row: Any) -> "CellStamp":
        """The stamp a cache row carries — all-unknown for a row from before
        this existed, or for one written by a path that stamps nothing."""
        if not isinstance(row, dict):
            return cls()
        policy = row.get("policy_hash")
        version = row.get("harness_version")
        return cls(
            policy if isinstance(policy, str) and policy else None,
            version if isinstance(version, str) and version else None,
        )

    @property
    def columns(self) -> tuple[str | None, str | None]:
        """``(policy_hash, harness_version)`` as the two nullable columns."""
        return (self.policy_hash, self.harness_version)


def divergence(stored: CellStamp, current: CellStamp) -> tuple[str, str] | None:
    """``(reason, message)`` when a replay is worth a mention, else None.

    An UNKNOWN on either side answers None for that field: a cell stored before
    the stamp existed, and a lookup by a reader that has no policy to compare
    against, are both "no record" — and inventing a divergence there would fault
    every replay of every run that predates this feature.

    ONE message per divergent replay even when BOTH fields moved: the count of
    advisories is what a certified template stamps, so two entries for one cell
    would publish a run as twice as advised as it was."""
    policy_moved = (
        stored.policy_hash is not None
        and current.policy_hash is not None
        and stored.policy_hash != current.policy_hash
    )
    version_moved = (
        stored.harness_version is not None
        and current.harness_version is not None
        and stored.harness_version != current.harness_version
    )
    if not policy_moved and not version_moved:
        return None
    versions = f"{stored.harness_version} → {current.harness_version}"
    if policy_moved and version_moved:
        return (
            REASON_POLICY_AND_HARNESS_VERSION_CHANGED,
            "replayed under a different sandbox policy and a different harness "
            f"version: {versions}{_ADVISORY_TAIL}",
        )
    if policy_moved:
        return (
            REASON_POLICY_CHANGED,
            "replayed under a different sandbox policy than the one it was "
            f"stored under{_ADVISORY_TAIL}",
        )
    return (
        REASON_HARNESS_VERSION_CHANGED,
        f"replayed under a different harness version: {versions}{_ADVISORY_TAIL}",
    )
