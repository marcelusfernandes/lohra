"""Orchestrate self-update: pre-flight checks, ff-pull, reinstall hint.

All the real-world outcomes are first-class statuses (not exceptions), because a
dev checkout hits 'dirty', 'no_upstream', or 'up_to_date' far more often than a
clean successful pull.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lohra.selfupdate import repo as gitrepo
from lohra.selfupdate.repo import GitError, GitRunner, default_git_runner

# (backend_dir) -> (ok, output); reinstalls the editable package.
PipRunner = Callable[[Path], "tuple[bool, str]"]

# Files whose change means the editable install must be refreshed.
_REINSTALL_TRIGGERS = ("pyproject.toml", "setup.py", "setup.cfg")

# status values
NOT_A_REPO = "not_a_repo"
DIRTY = "dirty"
NO_UPSTREAM = "no_upstream"
DIVERGED = "diverged"
UP_TO_DATE = "up_to_date"
BEHIND = "behind"
UPDATED = "updated"
ERROR = "error"

_OK_STATUSES = frozenset({UP_TO_DATE, BEHIND, UPDATED})


@dataclass(frozen=True)
class UpdateResult:
    status: str
    message: str
    changed_files: tuple[str, ...] = field(default_factory=tuple)
    reinstall_recommended: bool = False
    restart_required: bool = False

    @property
    def ok(self) -> bool:
        return self.status in _OK_STATUSES


def resolve_repo() -> Path | None:
    """Find the git checkout from the installed package location (not the CWD)."""
    import lohra

    return gitrepo.locate_repo(Path(lohra.__file__))


def _needs_reinstall(files: list[str]) -> bool:
    return any(Path(f).name in _REINSTALL_TRIGGERS for f in files)


def _default_pip_runner(backend_dir: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", str(backend_dir)],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def reinstall(repo: Path, pip_runner: PipRunner | None = None) -> tuple[bool, str]:
    """Reinstall the editable package from ``<repo>/backend``."""
    runner = pip_runner or _default_pip_runner
    return runner(repo / "backend")


def check_update(repo: Path, runner: GitRunner = default_git_runner) -> UpdateResult:
    """Network: fetch and report whether the checkout is behind its upstream."""
    branch = gitrepo.current_branch(runner, repo)
    if branch is None:
        return UpdateResult(NO_UPSTREAM, "HEAD is detached — checkout a branch to update.")
    if gitrepo.upstream_ref(runner, repo) is None:
        return UpdateResult(NO_UPSTREAM, f"no upstream configured for branch {branch!r}.")
    try:
        gitrepo.fetch(runner, repo)
        behind = gitrepo.behind_count(runner, repo)
    except GitError as exc:
        return UpdateResult(ERROR, f"could not check for updates: {exc}")
    if behind == 0:
        return UpdateResult(UP_TO_DATE, f"{branch} is up to date.")
    return UpdateResult(
        BEHIND, f"{branch} is {behind} commit(s) behind — run `lohra update` to apply."
    )


def perform_update(repo: Path, runner: GitRunner = default_git_runner) -> UpdateResult:
    """Pre-flight checks, then a fast-forward-only pull."""
    try:
        if gitrepo.is_dirty(runner, repo):
            return UpdateResult(
                DIRTY, "working tree has uncommitted changes — commit or stash before updating."
            )
        branch = gitrepo.current_branch(runner, repo)
        if branch is None:
            return UpdateResult(NO_UPSTREAM, "HEAD is detached — checkout a branch to update.")
        if gitrepo.upstream_ref(runner, repo) is None:
            return UpdateResult(NO_UPSTREAM, f"no upstream configured for branch {branch!r}.")
        old = gitrepo.current_sha(runner, repo)
        ok, output = gitrepo.pull_ff_only(runner, repo)
        if not ok:
            # Distinguish a real divergence from a generic failure structurally
            # (HEAD not an ancestor of the fetched upstream), not by matching
            # git's localized "fast-forward" message.
            if not gitrepo.is_ancestor(runner, repo, "HEAD", "@{u}"):
                return UpdateResult(
                    DIVERGED,
                    f"{branch} has diverged from its upstream — resolve manually.\n{output}",
                )
            return UpdateResult(ERROR, f"git pull failed:\n{output}")
        new = gitrepo.current_sha(runner, repo)
        if old == new:
            return UpdateResult(UP_TO_DATE, f"{branch} is already up to date.")
        files = gitrepo.changed_files(runner, repo, old, new)
    except GitError as exc:
        return UpdateResult(ERROR, f"update failed: {exc}")

    reinstall = _needs_reinstall(files)
    message = f"updated {branch}: {len(files)} file(s) changed ({old[:7]}..{new[:7]})."
    if reinstall:
        message += " Dependencies changed (pyproject)."
    return UpdateResult(
        UPDATED,
        message,
        changed_files=tuple(files),
        reinstall_recommended=reinstall,
        restart_required=True,
    )
