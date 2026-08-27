"""Leaf capability sandbox (spec §8.3) — the actual exfil mitigation.

The declarative reframe kills engine-escape but NOT leaf capability abuse: a
*valid* spec can still tell a leaf to read a secret and exfiltrate it via
``web_fetch``. Stock subagent isolation leaves ``read_file``/``write_file`` and
``web_fetch`` fully open. This wraps the subagent dispatch with, in order:

1. fs path-allowlist — reads/writes must resolve INSIDE the run's working_root
   (or an operator-allowed root); deny-by-default so ~/.lohra/.env etc. are out.
   An allowed root carries a MODE (WF-21): ``ro`` is readable but not writable,
   so letting leaves read a repo no longer lets them rewrite it. The run's own
   working_root is always read-write — it is the leaf's scratch space.
2. egress allowlist — ``web_fetch`` host must be in the operator policy (on top
   of the existing SSRF guard); default-deny for unattended runs.
3. taint — if the authoring context ingested untrusted content (web/MCP), the
   run is tainted and leaves get NO fs reads and NO web egress at all.

The policy lives in operator config (``~/.lohra/workflow_policy.json``), NEVER in
the workflow spec — an injected spec can't widen its own capability.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from lohra.tools.registry import tool_error

ToolDispatch = Callable[[str, dict], str]
ChildFactory = Callable[[], Any]

_FS_TOOLS = frozenset({"read_file", "write_file"})
_EGRESS_TOOLS = frozenset({"web_fetch", "web_search"})


_FS_MODES = {"ro": False, "rw": True}  # the only two an operator may write


@dataclass(frozen=True)
class FsRoot:
    """One operator-allowed root and whether leaves may WRITE under it."""

    path: Path
    writable: bool = True


def _as_root(entry: Any) -> FsRoot | None:
    """Normalise one authored allowlist entry, or None to DROP it.

    Two accepted shapes: a bare path (read-write — what every policy written
    before WF-21 meant, so an existing file keeps exactly the capability it
    already granted) and ``{"path": ..., "mode": "ro"|"rw"}``. Anything else —
    a typo'd mode, a missing or empty path (``Path("")`` is the CWD) — is
    dropped rather than guessed: deny-by-default all the way down.
    """
    if isinstance(entry, FsRoot):
        return entry
    if isinstance(entry, (str, Path)):
        raw, writable = entry, True
    elif isinstance(entry, dict):
        mode = entry.get("mode", "rw")
        raw = entry.get("path")
        if not isinstance(raw, (str, Path)) or mode not in _FS_MODES:
            return None
        writable = _FS_MODES[mode]
    else:
        return None
    return FsRoot(Path(raw).expanduser(), writable) if str(raw).strip() else None


@dataclass(frozen=True)
class WorkflowPolicy:
    """Operator-controlled capability policy (loaded from disk, not the spec).

    ``fs_allow`` entries are normalised to ``FsRoot`` on construction, so a
    caller may hand in bare paths, dicts, or FsRoots interchangeably."""

    fs_allow: tuple[FsRoot, ...] = field(default_factory=tuple)
    egress_allow: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        roots = tuple(r for r in (_as_root(e) for e in self.fs_allow) if r is not None)
        object.__setattr__(self, "fs_allow", roots)
        object.__setattr__(self, "egress_allow", tuple(self.egress_allow))

    def fs_roots(self, *, write: bool) -> tuple[Path, ...]:
        """The roots a read (or a write) may resolve inside."""
        return tuple(root.path for root in self.fs_allow if root.writable or not write)


def load_policy(path: Path) -> WorkflowPolicy:
    """Load ~/.lohra/workflow_policy.json; default-deny (empty) if absent/bad.

    ``{"fs_allow": ["/rw/root", {"path": "/ro/root", "mode": "ro"}],
       "egress_allow": ["api.test"]}`` — a bare string is read-write."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return WorkflowPolicy()
    fs_allow = data.get("fs_allow")
    egress = tuple(h for h in data.get("egress_allow", []) if isinstance(h, str))
    return WorkflowPolicy(
        fs_allow=tuple(fs_allow) if isinstance(fs_allow, list) else (), egress_allow=egress
    )


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _fs_allowed(raw_path: Any, roots: tuple[Path, ...]) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    target = Path(raw_path).expanduser()
    return any(_is_within(target, root) for root in roots)


def _fs_denial(
    name: str, raw_path: Any, working_root: Path, policy: WorkflowPolicy
) -> str | None:
    """None if this fs call is allowed, else WHY it is not (WF-21).

    The read-only case gets its own sentence: "outside the working scope" would
    send a leaf hunting for a path it can already read perfectly well."""
    write = name == "write_file"
    # working_root is the run's own scratch — always read-write.
    if _fs_allowed(raw_path, (working_root, *policy.fs_roots(write=write))):
        return None
    if write and _fs_allowed(raw_path, policy.fs_roots(write=False)):
        return "path is under a read-only workflow root (sandbox denied the write)"
    return "path is outside the workflow working scope (sandbox denied)"


def _egress_allowed(raw_url: Any, policy: WorkflowPolicy) -> bool:
    if not isinstance(raw_url, str):
        return False
    host = (urlparse(raw_url).hostname or "").lower()
    return host in {h.lower() for h in policy.egress_allow}


def sandbox_dispatch(
    base: ToolDispatch, *, working_root: Path, policy: WorkflowPolicy, tainted: bool
) -> ToolDispatch:
    """Wrap a (subagent) dispatch with the fs/egress allowlists + taint gate."""

    def dispatch(name: str, args: dict) -> str:
        if name in _FS_TOOLS:
            if tainted:
                return tool_error("tainted run: filesystem access is disabled for leaves")
            denial = _fs_denial(name, args.get("path"), working_root, policy)
            if denial is not None:
                return tool_error(denial)
        if name in _EGRESS_TOOLS:
            if tainted:
                return tool_error("tainted run: web egress is disabled for leaves")
            if name == "web_fetch" and not _egress_allowed(args.get("url"), policy):
                return tool_error("host is not in the workflow egress allowlist (sandbox denied)")
        return base(name, args)

    return dispatch


def make_sandboxed_leaf_factory(
    *,
    base_factory: ChildFactory,
    working_root: Path,
    policy: WorkflowPolicy,
    tainted: bool,
) -> ChildFactory:
    """Wrap an isolated-subagent factory so every leaf's dispatch is sandboxed."""

    def factory() -> Any:
        agent = base_factory()
        agent.tool_dispatch = sandbox_dispatch(
            agent.tool_dispatch, working_root=working_root, policy=policy, tainted=tainted
        )
        return agent

    return factory
