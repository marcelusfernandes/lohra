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
2. egress gates — ``web_fetch`` initial and redirect hosts must be allowlisted
   before DNS (on top of its SSRF guard); ``web_search`` requires ``allow_search``.
   Both default to denied. Search sends the query to its configured backend, independently
   of ``egress_allow``; its opt-in authorizes that search capability.
3. shell + MCP containment (issue #4, spec §8.3 control 4) — ``terminal`` and every ``mcp_*`` tool are
   DENIED by default. Stock subagent isolation left both wide open: the shell is
   guarded only by ``detect_dangerous_command``, which calls itself a speed-bump
   and happily runs ``cat ~/.lohra/.env`` or ``curl -d @/etc/passwd``, and MCP is
   an operator-configured egress the fs/egress allowlists never saw. Opt-in is
   the OPERATOR's (``allow_terminal`` / ``mcp_allow``), never the spec's.
4. taint (spec §8.2 control 3) — if the authoring context ingested untrusted content (web/MCP), the
   run is tainted and leaves get NO fs reads, NO web egress, NO shell and NO MCP
   at all — the opt-ins do not override taint.

The policy lives in operator config (``~/.lohra/workflow_policy.json``) plus three
env vars (``LOHRA_LEAF_ALLOW_TERMINAL``, ``LOHRA_LEAF_MCP_ALLOW``,
``LOHRA_LEAF_ALLOW_SEARCH``), NEVER in the
workflow spec — an injected spec can't widen its own capability. ``fs_allow`` and
``egress_allow`` are on the same footing: an authored ``fs_allow`` field on a node
is not a thing, and shell/MCP could not be one even in principle — a leaf that
may run a shell has, transitively, every capability the sandbox denies above it.

NAMED residual: an ordinary non-MCP entry whose name is outside the other gated
classes (fs, egress, ``terminal``, ``mcp_*``) and not marked author-time-only passes
to ``subagent_dispatch``, which applies its own legacy exclusions. Gating unknown names here by
default would break every ordinary stateless tool added to the registry later,
so the containment is per capability class, deliberately.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from lohra.mcp.tools import MCP_PREFIX, mcp_server_slug
from lohra.tools.author_scope import author_time_denial
from lohra.tools.registry import (
    ToolEntry, ToolRegistry, bind_dispatch_guard, dispatch_denial, registry,
)
from lohra.tools.sandbox_denials import denied
from lohra.web.egress import RestrictedFetchArgs, host_allowed

logger = logging.getLogger(__name__)

ToolDispatch = Callable[[str, dict], str]
ChildFactory = Callable[[], Any]

_FS_TOOLS = frozenset({"read_file", "write_file"})
_EGRESS_TOOLS = frozenset({"web_fetch", "web_search"})
_TERMINAL_TOOL = "terminal"

# Operator env surfaces (issue #4). They only ever WIDEN the file policy, and
# only through ``load_policy`` — a caller that hands ``WorkflowService`` an
# explicit ``policy=`` object gets exactly that object, env included or not.
ENV_ALLOW_TERMINAL = "LOHRA_LEAF_ALLOW_TERMINAL"
ENV_MCP_ALLOW = "LOHRA_LEAF_MCP_ALLOW"
ENV_ALLOW_SEARCH = "LOHRA_LEAF_ALLOW_SEARCH"
_TRUE_VALUES = frozenset({"1", "on", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "off", "false", "no"})

_TERMINAL_DENIAL = (
    "the 'terminal' tool is disabled for workflow leaves (sandbox denied) — an "
    'operator may enable it with {"allow_terminal": true} in '
    f"~/.lohra/workflow_policy.json or {ENV_ALLOW_TERMINAL}=1"
)
_SEARCH_DENIAL = (
    "the 'web_search' tool is disabled for workflow leaves (sandbox denied) — an "
    'operator may enable it with {"allow_search": true} in '
    f"~/.lohra/workflow_policy.json or {ENV_ALLOW_SEARCH}=1"
)


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
    # Shell, MCP and search are OFF unless the operator opts in (#4/#55).
    allow_terminal: bool = False
    mcp_allow: tuple[str, ...] = field(default_factory=tuple)
    allow_search: bool = False

    def __post_init__(self) -> None:
        roots = tuple(r for r in (_as_root(e) for e in self.fs_allow) if r is not None)
        object.__setattr__(self, "fs_allow", roots)
        object.__setattr__(self, "egress_allow", tuple(self.egress_allow))
        object.__setattr__(self, "allow_terminal", self.allow_terminal is True)
        object.__setattr__(self, "mcp_allow", _mcp_servers(self.mcp_allow))
        object.__setattr__(self, "allow_search", self.allow_search is True)

    def mcp_tool_allowed(self, name: str, entry: ToolEntry | None = None) -> bool:
        """Only a registered entry's exact original server can supply authority."""
        return (entry is not None and entry.name == name and entry.toolset.startswith("mcp-")
                and entry.toolset[4:] in self.mcp_allow)

    def fs_roots(self, *, write: bool) -> tuple[Path, ...]:
        """The roots a read (or a write) may resolve inside."""
        return tuple(root.path for root in self.fs_allow if root.writable or not write)


def _mcp_servers(entries: Any) -> tuple[str, ...]:
    """Keep exact identities, dedupe and drop junk; slugs only test validity."""
    if not isinstance(entries, (list, tuple)):
        return ()
    seen: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        if mcp_server_slug(entry) and entry not in seen:
            seen.append(entry)
    return tuple(seen)


def _env_opt_in(name: str, capability: str) -> bool:
    """Operator boolean in the ``LOHRA_AUDIT`` pattern: garbage → off."""
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return False
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False  # an operator spelling the OFF value out is not a mistake
    logger.warning(
        "ignoring %s=%r: expected 1/on/true/yes; leaves keep no %s", name, raw, capability
    )
    return False


def _env_mcp_allow() -> tuple[str, ...]:
    """``LOHRA_LEAF_MCP_ALLOW=srv1,srv2`` — comma-separated server names."""
    raw = os.environ.get(ENV_MCP_ALLOW) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_policy(path: Path) -> WorkflowPolicy:
    """Load ~/.lohra/workflow_policy.json; default-deny (empty) if absent/bad.

    ``{"fs_allow": ["/rw/root", {"path": "/ro/root", "mode": "ro"}],
       "egress_allow": ["api.test"], "allow_terminal": false, "allow_search": false,
       "mcp_allow": ["srv"]}`` — a bare fs string is read-write.

    ``allow_terminal`` and ``allow_search`` require real JSON booleans: the
    string ``"false"`` is truthy in Python and would grant capability, so anything but
    ``true`` is dropped rather than guessed. The env vars are merged on BOTH
    paths (file present or not) and can only widen — an operator must be able to
    opt in for one process without editing shared config."""
    data: Any = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, ValueError):
        data = {}
    fs_allow = data.get("fs_allow")
    egress = tuple(h for h in data.get("egress_allow", []) if isinstance(h, str))
    return WorkflowPolicy(
        fs_allow=tuple(fs_allow) if isinstance(fs_allow, list) else (),
        egress_allow=egress,
        allow_terminal=data.get("allow_terminal") is True or _env_opt_in(ENV_ALLOW_TERMINAL, "shell"),
        mcp_allow=_mcp_servers(data.get("mcp_allow")) + _env_mcp_allow(),
        allow_search=data.get("allow_search") is True or _env_opt_in(ENV_ALLOW_SEARCH, "search"),
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
    """None if allowed, else the canonical refusal code (WF-21, #89).

    The read-only case gets its own sentence: "outside the working scope" would
    send a leaf hunting for a path it can already read perfectly well."""
    write = name == "write_file"
    # working_root is the run's own scratch — always read-write.
    if _fs_allowed(raw_path, (working_root, *policy.fs_roots(write=write))):
        return None
    if write and _fs_allowed(raw_path, policy.fs_roots(write=False)):
        return "fs_read_only"
    return "fs_outside_scope"


def _mcp_denial(
    name: str, entry: ToolEntry | None, *, policy: WorkflowPolicy, tainted: bool
) -> str | None:
    """One predicate for advertisement, preflight and the actual selected entry."""
    if not (name.startswith(MCP_PREFIX) or (entry and entry.toolset.startswith("mcp-"))):
        return None
    if tainted:
        return denied(name, "tainted_mcp")
    if policy.mcp_tool_allowed(name, entry):
        return None
    return denied(
        name, "mcp_not_allowed",
        f"the {name!r} MCP tool is not in the workflow leaf allowlist (sandbox "
        'denied) — an operator may allow its registered server with {"mcp_allow": '
        f'["<server>"]}} in ~/.lohra/workflow_policy.json or {ENV_MCP_ALLOW}=<server>'
    )


def sandbox_dispatch(
    base: ToolDispatch, *, working_root: Path, policy: WorkflowPolicy, tainted: bool,
    tool_registry: ToolRegistry | None = None,
) -> ToolDispatch:
    """Wrap a (subagent) dispatch with the fs/egress/shell/MCP gates + taint.

    Note what remains UNDER this wrapper when ``allow_terminal`` is on: the only
    guard left on the shell is ``subagent_dispatch``'s ``detect_dangerous_command``
    auto-deny, which is a bypassable denylist heuristic by its own admission. The
    opt-in is therefore an operator decision to trust the specs they run."""

    catalog = tool_registry if tool_registry is not None else registry

    def guard(name: str, entry: ToolEntry | None) -> str | None:
        return author_time_denial(name, entry) or _mcp_denial(
            name, entry, policy=policy, tainted=tainted
        )

    def dispatch(name: str, args: dict) -> str:
        # Reject missing provenance even when base is an opaque interceptor.
        # This is not execution authority: registry.dispatch checks all active
        # guards again on the immutable entry whose handler it actually invokes.
        refusal = dispatch_denial(name, catalog.entry(name))
        if refusal is not None:
            return refusal
        if name == _TERMINAL_TOOL:
            # Taint first, and with no remedy in the message: there is no override.
            if tainted:
                return denied(name, "tainted_terminal")
            if not policy.allow_terminal:
                return denied(name, "terminal_disabled", _TERMINAL_DENIAL)
        if name in _FS_TOOLS:
            if tainted:
                return denied(name, "tainted_fs")
            denial = _fs_denial(name, args.get("path"), working_root, policy)
            if denial is not None:
                return denied(name, denial)
        if name in _EGRESS_TOOLS:
            if tainted:
                return denied(name, "tainted_egress")
            if name == "web_search" and not policy.allow_search:
                return denied(name, "search_disabled", _SEARCH_DENIAL)
            if name == "web_fetch":
                if not host_allowed(args.get("url"), policy.egress_allow):
                    return denied(name, "egress_not_allowed")
                # Copy caller args, replacing any earlier internal provenance.
                # JSON keys cannot create/override the typed host restriction.
                return base(name, RestrictedFetchArgs(args, policy.egress_allow))
        return base(name, args)

    return bind_dispatch_guard(dispatch, guard)


def _capability_denied(
    name: str, entry: ToolEntry | None, *, policy: WorkflowPolicy, tainted: bool
) -> bool:
    """True when ``sandbox_dispatch`` would refuse this registered tool outright.

    Only whole-tool gates (author-time, shell, MCP, search) answer here — fs/fetch denials
    depend on the call's arguments, so those tools stay visible and are judged
    per call."""
    if author_time_denial(name, entry) is not None:
        return True
    if _mcp_denial(name, entry, policy=policy, tainted=tainted) is not None:
        return True
    if name == _TERMINAL_TOOL:
        return tainted or not policy.allow_terminal
    if name == "web_search":
        return tainted or not policy.allow_search
    return False


def sandbox_tool_definitions(
    definitions: tuple[dict, ...], *, policy: WorkflowPolicy, tainted: bool,
    tool_registry: ToolRegistry | None = None,
) -> tuple[dict, ...]:
    """Filter by registered identity as well as the reserved capability names.

    Defense in depth on BOTH surfaces, exactly like ``delegate.py`` strips
    ``_CHILD_EXCLUDED_TOOLS`` from the child's definitions AND refuses them in
    the dispatch: a leaf that can see ``terminal`` will call it, eat a
    ``tool_error`` and burn an iteration off its 50-cap for nothing. Returns a
    new tuple — the parent's definitions are never mutated."""
    catalog = tool_registry if tool_registry is not None else registry
    return tuple(
        d
        for d in definitions
        if not _capability_denied(
            name := d.get("function", {}).get("name", ""), catalog.entry(name),
            policy=policy, tainted=tainted
        )
    )


def make_sandboxed_leaf_factory(
    *,
    base_factory: ChildFactory,
    working_root: Path,
    policy: WorkflowPolicy,
    tainted: bool,
    tool_registry: ToolRegistry | None = None,
) -> ChildFactory:
    """Wrap an isolated-subagent factory so every leaf is sandboxed.

    Both surfaces: the dispatch enforces the gates, and the tool definitions
    stop advertising what the dispatch would refuse."""

    def factory() -> Any:
        agent = base_factory()
        agent.tool_dispatch = sandbox_dispatch(
            agent.tool_dispatch, working_root=working_root, policy=policy, tainted=tainted,
            tool_registry=tool_registry,
        )
        definitions = getattr(agent, "tool_definitions", ())
        if definitions:
            agent.tool_definitions = sandbox_tool_definitions(
                tuple(definitions), policy=policy, tainted=tainted, tool_registry=tool_registry
            )
        return agent

    return factory
