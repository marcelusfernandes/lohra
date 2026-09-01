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
3. shell + MCP containment (issue #4, spec §8.3 control 4) — ``terminal`` and every ``mcp_*`` tool are
   DENIED by default. Stock subagent isolation left both wide open: the shell is
   guarded only by ``detect_dangerous_command``, which calls itself a speed-bump
   and happily runs ``cat ~/.lohra/.env`` or ``curl -d @/etc/passwd``, and MCP is
   an operator-configured egress the fs/egress allowlists never saw. Opt-in is
   the OPERATOR's (``allow_terminal`` / ``mcp_allow``), never the spec's.
4. taint (spec §8.2 control 3) — if the authoring context ingested untrusted content (web/MCP), the
   run is tainted and leaves get NO fs reads, NO web egress, NO shell and NO MCP
   at all — the opt-ins do not override taint.

The policy lives in operator config (``~/.lohra/workflow_policy.json``) plus two
env vars (``LOHRA_LEAF_ALLOW_TERMINAL``, ``LOHRA_LEAF_MCP_ALLOW``), NEVER in the
workflow spec — an injected spec can't widen its own capability. ``fs_allow`` and
``egress_allow`` are on the same footing: an authored ``fs_allow`` field on a node
is not a thing, and shell/MCP could not be one even in principle — a leaf that
may run a shell has, transitively, every capability the sandbox denies above it.

NAMED residual: a tool name outside the four gated classes (fs, egress,
``terminal``, ``mcp_*``) still passes through to ``subagent_dispatch``, which
applies its own ``_CHILD_EXCLUDED_TOOLS`` refusal. Gating unknown names here by
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
from urllib.parse import urlparse

from lohra.mcp.tools import MCP_PREFIX, mcp_server_slug
from lohra.tools.registry import tool_error

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
_TRUE_VALUES = frozenset({"1", "on", "true", "yes"})
_FALSE_VALUES = frozenset({"0", "off", "false", "no"})

_TERMINAL_DENIAL = (
    "the 'terminal' tool is disabled for workflow leaves (sandbox denied) — an "
    'operator may enable it with {"allow_terminal": true} in '
    f"~/.lohra/workflow_policy.json or {ENV_ALLOW_TERMINAL}=1"
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
    # Shell + MCP are OFF unless the operator says otherwise (issue #4).
    allow_terminal: bool = False
    mcp_allow: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        roots = tuple(r for r in (_as_root(e) for e in self.fs_allow) if r is not None)
        object.__setattr__(self, "fs_allow", roots)
        object.__setattr__(self, "egress_allow", tuple(self.egress_allow))
        object.__setattr__(self, "allow_terminal", self.allow_terminal is True)
        object.__setattr__(self, "mcp_allow", _mcp_servers(self.mcp_allow))

    def mcp_tool_allowed(self, name: str) -> bool:
        """True when ``name`` belongs to a server the operator allowlisted.

        Match is on the FULL server segment (``mcp_{server}_``), never a loose
        prefix: an entry ``git`` must not silently cover a ``github`` server.
        Server names are slugged exactly as ``mcp_tool_name`` slugs them, so an
        operator may write the server as it appears in ``mcp.json``.

        NAMED residual: the registry name joins server and tool with the same
        ``_`` the slugs use internally, so it is not unambiguously parseable —
        allowing ``github`` also matches ``mcp_github_enterprise_search`` from a
        ``github-enterprise`` server. Deny-by-default holds (nothing opens
        without an opt-in), but an opt-in can be wider than declared. Closing it
        needs the registry's ``mcp-{server}`` toolset, which this layer has no
        handle on."""
        return any(name.startswith(f"{MCP_PREFIX}{server}_") for server in self.mcp_allow)

    def fs_roots(self, *, write: bool) -> tuple[Path, ...]:
        """The roots a read (or a write) may resolve inside."""
        return tuple(root.path for root in self.fs_allow if root.writable or not write)


def _mcp_servers(entries: Any) -> tuple[str, ...]:
    """Normalise MCP server names: slugged, deduped, junk dropped (deny-by-default)."""
    if not isinstance(entries, (list, tuple)):
        return ()
    seen: list[str] = []
    for entry in entries:
        if not isinstance(entry, str):
            continue
        slug = mcp_server_slug(entry)
        if slug and slug not in seen:
            seen.append(slug)
    return tuple(seen)


def _env_allow_terminal() -> bool:
    """``LOHRA_LEAF_ALLOW_TERMINAL`` in the ``LOHRA_AUDIT`` pattern: garbage → off."""
    raw = (os.environ.get(ENV_ALLOW_TERMINAL) or "").strip().lower()
    if not raw:
        return False
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False  # an operator spelling the OFF value out is not a mistake
    logger.warning(
        "ignoring %s=%r: expected 1/on/true/yes; leaves keep no shell", ENV_ALLOW_TERMINAL, raw
    )
    return False


def _env_mcp_allow() -> tuple[str, ...]:
    """``LOHRA_LEAF_MCP_ALLOW=srv1,srv2`` — comma-separated server names."""
    raw = os.environ.get(ENV_MCP_ALLOW) or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_policy(path: Path) -> WorkflowPolicy:
    """Load ~/.lohra/workflow_policy.json; default-deny (empty) if absent/bad.

    ``{"fs_allow": ["/rw/root", {"path": "/ro/root", "mode": "ro"}],
       "egress_allow": ["api.test"], "allow_terminal": false,
       "mcp_allow": ["srv"]}`` — a bare fs string is read-write.

    ``allow_terminal`` must be a real JSON boolean: the string ``"false"`` is
    truthy in Python and would silently hand every leaf a shell, so anything but
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
        allow_terminal=data.get("allow_terminal") is True or _env_allow_terminal(),
        mcp_allow=_mcp_servers(data.get("mcp_allow")) + _env_mcp_allow(),
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
    """Wrap a (subagent) dispatch with the fs/egress/shell/MCP gates + taint.

    Note what remains UNDER this wrapper when ``allow_terminal`` is on: the only
    guard left on the shell is ``subagent_dispatch``'s ``detect_dangerous_command``
    auto-deny, which is a bypassable denylist heuristic by its own admission. The
    opt-in is therefore an operator decision to trust the specs they run."""

    def dispatch(name: str, args: dict) -> str:
        if name == _TERMINAL_TOOL:
            # Taint first, and with no remedy in the message: there is no override.
            if tainted:
                return tool_error("tainted run: shell access is disabled for leaves")
            if not policy.allow_terminal:
                return tool_error(_TERMINAL_DENIAL)
        if name.startswith(MCP_PREFIX):
            if tainted:
                return tool_error("tainted run: MCP tools are disabled for leaves")
            if not policy.mcp_tool_allowed(name):
                return tool_error(
                    f"the {name!r} MCP tool is not in the workflow leaf allowlist (sandbox "
                    'denied) — an operator may allow its server with {"mcp_allow": '
                    f'["<server>"]}} in ~/.lohra/workflow_policy.json or {ENV_MCP_ALLOW}=<server>'
                )
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


def _capability_denied(name: str, *, policy: WorkflowPolicy, tainted: bool) -> bool:
    """True when ``sandbox_dispatch`` would refuse this tool NAME outright.

    Only the whole-tool gates (shell, MCP) answer here — fs/egress denials
    depend on the call's arguments, so those tools stay visible and are judged
    per call."""
    if name == _TERMINAL_TOOL:
        return tainted or not policy.allow_terminal
    if name.startswith(MCP_PREFIX):
        return tainted or not policy.mcp_tool_allowed(name)
    return False


def sandbox_tool_definitions(
    definitions: tuple[dict, ...], *, policy: WorkflowPolicy, tainted: bool
) -> tuple[dict, ...]:
    """Drop the definitions of tools the sandbox would refuse by name.

    Defense in depth on BOTH surfaces, exactly like ``delegate.py`` strips
    ``_CHILD_EXCLUDED_TOOLS`` from the child's definitions AND refuses them in
    the dispatch: a leaf that can see ``terminal`` will call it, eat a
    ``tool_error`` and burn an iteration off its 50-cap for nothing. Returns a
    new tuple — the parent's definitions are never mutated."""
    return tuple(
        d
        for d in definitions
        if not _capability_denied(
            d.get("function", {}).get("name", ""), policy=policy, tainted=tainted
        )
    )


def make_sandboxed_leaf_factory(
    *,
    base_factory: ChildFactory,
    working_root: Path,
    policy: WorkflowPolicy,
    tainted: bool,
) -> ChildFactory:
    """Wrap an isolated-subagent factory so every leaf is sandboxed.

    Both surfaces: the dispatch enforces the gates, and the tool definitions
    stop advertising what the dispatch would refuse."""

    def factory() -> Any:
        agent = base_factory()
        agent.tool_dispatch = sandbox_dispatch(
            agent.tool_dispatch, working_root=working_root, policy=policy, tainted=tainted
        )
        definitions = getattr(agent, "tool_definitions", ())
        if definitions:
            agent.tool_definitions = sandbox_tool_definitions(
                tuple(definitions), policy=policy, tainted=tainted
            )
        return agent

    return factory
