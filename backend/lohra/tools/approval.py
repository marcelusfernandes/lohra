"""Approval gate for dangerous commands (spec §5).

A regex list classifies a command; the ApprovalManager consults cached
session approvals, a yolo override, and a pluggable callback (CLI prompt or
gateway queue). Fail-safe: an unclassifiable approval (no callback, or a
callback that errors) denies.

⚠️  SECURITY SCOPE. This denylist is a best-effort SPEED-BUMP against common
destructive mistakes — NOT a security sandbox. A determined or adversarial
command can evade regex matching (novel tools, obfuscation, command chaining,
encoded payloads). Real isolation against untrusted use is the container/ssh
terminal backend's job (spec §7), not this gate. The filesystem tools
(read_file/write_file) are likewise unsandboxed and run with the operator's
full privileges. Run Lohra only on inputs you trust at the privilege level it
runs with.

Phase 2 supports the choices "once" | "session" | "deny" plus a yolo mode.
"session"/"always" cache the EXACT command (not its category), so approving
one `rm -rf <dir>` never silently auto-approves a different `rm -rf <other>`.
Durable cross-session allow-lists land with the config store in a later phase.
"""

from __future__ import annotations

import re
import threading
from typing import Callable

# (key, pattern, human description). Matched case-insensitively against the
# command string (whole string, so chained `safe; dangerous` is caught).
# Ordered roughly most-destructive first.
_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # recursive rm in any flag form: -rf, -fr, -r -f, --recursive [--force]
    ("rm_rf", r"\brm\s+(?:-{1,2}\S+\s+)*(?:-\w*r\w*|--recursive)\b", "recursive delete (rm -r)"),
    ("fork_bomb", r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "shell fork bomb"),
    ("dd_disk", r"\bdd\s+.*\b(if|of)=/dev/", "raw disk write (dd to /dev)"),
    ("redirect_device", r">\s*/dev/(sd|nvme|disk|hd|mmcblk)", "redirect to a block device"),
    ("mkfs", r"\bmkfs(\.\w+)?\b", "filesystem format (mkfs)"),
    ("find_delete", r"\bfind\b[^\n]*\s-delete\b", "bulk delete via find -delete"),
    ("shred", r"\bshred\b", "secure file shredding (shred)"),
    ("chmod_perm", r"\bchmod\s+(-[a-z]+\s+)*[0-7]*7[0-7]{2}\b", "broad permission change (chmod ...7xx)"),
    ("chown_root", r"\bchown\s+(-[a-z]+\s+)*root\b", "change ownership to root"),
    ("pipe_to_shell", r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b", "download piped into a shell"),
    ("sql_drop", r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "destructive SQL (DROP)"),
    (
        "git_force_push",
        r"\bgit\s+push\b[^\n]*(--force\b|--force-with-lease\b|\s-f\b|\s\+\S+)",
        "force push (rewrites history)",
    ),
    ("sudo", r"\bsudo\b", "elevated privileges (sudo)"),
)

DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = tuple(
    (key, re.compile(pattern, re.IGNORECASE), desc) for key, pattern, desc in _PATTERNS
)

# (command, description, *, allow_permanent) -> "once" | "session" | "always" | "deny"
ApprovalCallback = Callable[..., str]


def detect_dangerous_command(command: str) -> tuple[bool, str | None, str | None]:
    """Return (is_dangerous, pattern_key, description) for the first match."""
    for key, pattern, desc in DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True, key, desc
    return False, None, None


class ApprovalManager:
    """Thread-safe approval state with a pluggable decision callback."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_approved: set[str] = set()
        self._yolo = False
        self._callback: ApprovalCallback | None = None

    def set_callback(self, callback: ApprovalCallback | None) -> None:
        with self._lock:
            self._callback = callback

    def set_yolo(self, enabled: bool) -> None:
        with self._lock:
            self._yolo = enabled

    def reset(self) -> None:
        """Clear session approvals (e.g. between sessions)."""
        with self._lock:
            self._session_approved.clear()

    def require(self, command: str) -> bool:
        """Return True if the command may run. Fail-safe: deny on any ambiguity."""
        is_dangerous, _key, desc = detect_dangerous_command(command)
        if not is_dangerous:
            return True

        with self._lock:
            if self._yolo:
                return True
            if command in self._session_approved:  # cache the EXACT command, not its category
                return True
            callback = self._callback

        if callback is None:
            return False  # nothing to ask -> deny

        try:
            choice = callback(command, desc, allow_permanent=True)
        except Exception:
            return False  # a broken approver must not unblock a dangerous command

        if choice in ("session", "always"):
            with self._lock:
                self._session_approved.add(command)
            return True
        return choice == "once"


# Module-level singleton — tools and the CLI share one approval state.
approval = ApprovalManager()
