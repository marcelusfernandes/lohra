"""Resolution of the Lohra home directory and its layout.

The base root is ``~/.lohra`` (overridable via ``LOHRA_HOME``). An optional
*profile* (``LOHRA_PROFILE``, or ``lohra --profile <name>``) re-roots ALL state
under ``<base>/profiles/<name>/`` — memory, skills, sessions (state.db), cron,
mcp.json, generated images. Every subsystem already resolves through
``lohra_home()`` with no profile argument, so making that one function
profile-aware isolates everything by construction.

Backward-compatible: with no profile active, ``lohra_home()`` is exactly the
base (``~/.lohra``), so existing installs keep their data in place.

See docs/specs/03-memory-skills-state.md §7.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# A profile name is a path component: an anchored allowlist, length-capped, no
# separators or dots — so it can never traverse out of ``<base>/profiles/``.
_PROFILE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_MAX_PROFILE_LEN = 64

_SUBDIRS = ("memories", "skills", "cron", "logs", "plugins")


def validate_profile_name(name: str) -> str:
    """Return ``name`` if it is a safe path component, else raise ``ValueError``."""
    if not name or len(name) > _MAX_PROFILE_LEN or not _PROFILE_PATTERN.fullmatch(name):
        raise ValueError(
            f"invalid profile name {name!r}: use letters, digits, '-' or '_' "
            f"(1-{_MAX_PROFILE_LEN} chars, no spaces or path separators)"
        )
    return name


def lohra_base() -> Path:
    """The profile-independent root (``~/.lohra`` or ``$LOHRA_HOME``), not created."""
    env = os.environ.get("LOHRA_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", str(Path.home()))
        return Path(base) / "lohra"
    return Path.home() / ".lohra"


def active_profile() -> str | None:
    """The validated active profile from ``LOHRA_PROFILE``, or None.

    Validation happens here, on the env read path, because ``LOHRA_PROFILE`` can
    be set out-of-band — the value becomes a path component regardless of how it
    arrived, so it must be checked before it is trusted.
    """
    name = os.environ.get("LOHRA_PROFILE")
    if not name:
        return None
    return validate_profile_name(name)


def profiles_dir() -> Path:
    """The directory holding all profile workspaces (``<base>/profiles``)."""
    return lohra_base() / "profiles"


def lohra_home() -> Path:
    """The effective home: the base, or ``<base>/profiles/<name>`` if a profile is active."""
    profile = active_profile()
    return profiles_dir() / profile if profile else lohra_base()


def list_profiles() -> list[str]:
    """Names of existing profile workspaces (sorted), or [] if none."""
    root = profiles_dir()
    if not root.is_dir():
        return []
    return sorted(child.name for child in root.iterdir() if child.is_dir())


def ensure_home() -> Path:
    """Create the effective home and its subdirectories; return it."""
    root = lohra_home()
    for sub in _SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def state_db_path() -> Path:
    return ensure_home() / "state.db"


def soul_path() -> Path:
    return ensure_home() / "SOUL.md"


def mcp_config_path() -> Path:
    """Path to the MCP server config (``<home>/mcp.json``). Not created."""
    return lohra_home() / "mcp.json"
