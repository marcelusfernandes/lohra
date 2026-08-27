"""Git primitives behind an injectable runner (testable offline).

Every git call goes through a ``GitRunner`` so the orchestration in service.py
can be exercised with scripted responses — no real repo, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

# (args, cwd) -> (returncode, stdout, stderr); stdout/stderr already stripped.
GitRunner = Callable[[list[str], str], "tuple[int, str, str]"]


class GitError(RuntimeError):
    """A git command failed unexpectedly (not an expected state like 'diverged')."""


def default_git_runner(args: list[str], cwd: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True  # noqa: S607 - git on PATH
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def locate_repo(start: Path) -> Path | None:
    """Walk up from ``start`` to the directory containing ``.git``; None if none."""
    start = start.resolve()
    for path in (start, *start.parents):
        if (path / ".git").exists():
            return path
    return None


def _run(runner: GitRunner, repo: Path, args: list[str]) -> tuple[int, str, str]:
    return runner(args, str(repo))


def current_sha(runner: GitRunner, repo: Path) -> str:
    code, out, err = _run(runner, repo, ["rev-parse", "HEAD"])
    if code != 0:
        raise GitError(err or "git rev-parse HEAD failed")
    return out


def is_dirty(runner: GitRunner, repo: Path) -> bool:
    code, out, err = _run(runner, repo, ["status", "--porcelain"])
    if code != 0:
        raise GitError(err or "git status failed")
    return bool(out.strip())


def current_branch(runner: GitRunner, repo: Path) -> str | None:
    """The checked-out branch, or None if HEAD is detached."""
    code, out, _ = _run(runner, repo, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    return out or None if code == 0 else None


def upstream_ref(runner: GitRunner, repo: Path) -> str | None:
    """The tracking ref (e.g. 'origin/main'), or None if none is configured."""
    code, out, _ = _run(
        runner, repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    return out or None if code == 0 else None


def fetch(runner: GitRunner, repo: Path) -> None:
    code, _, err = _run(runner, repo, ["fetch", "--quiet"])
    if code != 0:
        raise GitError(err or "git fetch failed")


def behind_count(runner: GitRunner, repo: Path) -> int:
    """How many commits upstream is ahead of the local HEAD."""
    code, out, err = _run(runner, repo, ["rev-list", "--count", "HEAD..@{u}"])
    if code != 0:
        raise GitError(err or "git rev-list failed")
    try:
        return int(out or "0")
    except ValueError as exc:  # keep the GitError contract callers catch
        raise GitError(f"unexpected rev-list output: {out!r}") from exc


def is_ancestor(runner: GitRunner, repo: Path, ancestor: str, descendant: str) -> bool:
    """Whether ``ancestor`` is an ancestor of ``descendant`` (structural, no text
    parsing) — used to tell a real divergence from a generic pull failure."""
    code, _, _ = _run(runner, repo, ["merge-base", "--is-ancestor", ancestor, descendant])
    return code == 0


def pull_ff_only(runner: GitRunner, repo: Path) -> tuple[bool, str]:
    """Fast-forward pull. Returns (ok, combined_output)."""
    code, out, err = _run(runner, repo, ["pull", "--ff-only"])
    combined = "\n".join(part for part in (out, err) if part).strip()
    return code == 0, combined


def changed_files(runner: GitRunner, repo: Path, old: str, new: str) -> list[str]:
    code, out, err = _run(runner, repo, ["diff", "--name-only", f"{old}..{new}"])
    if code != 0:
        raise GitError(err or "git diff failed")
    return [line for line in out.splitlines() if line.strip()]
