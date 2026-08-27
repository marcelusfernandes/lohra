"""Discover the project root + its agent-instruction files (Fase 9, A1).

AGENTS.md is the emerging cross-tool standard; CLAUDE.md is Anthropic's. Both are
read (AGENTS first), walking up from the cwd to the project root, so a project's
instructions reach Lohra's frozen system prompt and it follows them.

Hardened (ultracode review): the root is the VCS/agent root so a sub-package
build manifest (backend/pyproject.toml) doesn't amputate discovery of an outer
repo's CLAUDE.md; reads are bounded + reject symlinks/non-regular files (no
OOM/hang, no symlink-escape exfiltration); and the public entrypoint can never
raise (a discovery failure degrades to empty context, never breaks a session).
"""

from __future__ import annotations

from pathlib import Path

from lohra.safeio import read_text_bounded

# VCS / agent roots — where instructions live; terminal for the walk.
_VCS_MARKERS = (".git", ".hg", ".claude")
# Build manifests — a root only as a FALLBACK when no VCS root is found above
# (they routinely sit BELOW the repo root in monorepos).
_BUILD_MARKERS = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml")
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")  # precedence order
_MAX_CHARS = 32_000  # per instruction file; the prompt has a budget
_MAX_BYTES = _MAX_CHARS * 4  # hard read cap (bounds memory regardless of file size)
_MAX_WALK = 25  # defensive cap on the upward walk


def find_project_root(start: Path) -> Path:
    """The project root: the nearest ancestor with a VCS/agent marker; failing
    that, the nearest with a build manifest; failing that, ``start`` itself.

    VCS markers win so a sub-package manifest never shadows the repo's
    instructions in a monorepo (the dogfood `cd backend` case)."""
    ancestors = (start.resolve(), *start.resolve().parents)[:_MAX_WALK]
    for directory in ancestors:
        if any((directory / m).exists() for m in _VCS_MARKERS):
            return directory
    for directory in ancestors:
        if any((directory / m).exists() for m in _BUILD_MARKERS):
            return directory
    return ancestors[0]


def _read_capped(path: Path) -> str | None:
    """Symlink-safe, bounded instruction read, char-capped for the prompt budget."""
    text = read_text_bounded(path, _MAX_BYTES)
    if text is None:
        return None
    if len(text) > _MAX_CHARS:
        return text[:_MAX_CHARS] + "\n\n[...truncated]"
    return text


def discover_instructions(cwd: Path, root: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Instruction files from ``cwd`` up to the project root, nearest first, each
    file type once. Returns (label, content) pairs for the prompt's context tier."""
    cwd = cwd.resolve()
    root = (root or find_project_root(cwd)).resolve()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in (cwd, *cwd.parents):
        for name in INSTRUCTION_FILES:
            if name in seen:
                continue
            content = _read_capped(directory / name)
            if content is not None:
                found.append((_label(directory / name, root), content))
                seen.add(name)
        if directory == root:
            break
    return tuple(found)


def _label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def environment_hints(cwd: Path, root: Path | None = None) -> dict[str, str]:
    """Stable env hints for the prompt: where the agent is + the project root."""
    cwd = cwd.resolve()
    root = (root or find_project_root(cwd)).resolve()
    return {"cwd": str(cwd), "project_root": str(root)}


# Where a project keeps its skills (relative to the project root).
_PROJECT_SKILL_DIRS = (".claude/skills", ".lohra/skills")


def discover_skill_roots(cwd: Path) -> tuple[Path, ...]:
    """Existing project skill dirs (`.claude/skills`, `.lohra/skills`) under the
    project root — for the SkillStore to scan alongside the home skills."""
    try:
        root = find_project_root(cwd)
    except Exception:
        return ()
    return tuple(root / rel for rel in _PROJECT_SKILL_DIRS if (root / rel).is_dir())


def load_project_context(cwd: Path) -> tuple[tuple[tuple[str, str], ...], dict[str, str]]:
    """One call for a session: (context_files, environment_hints). NEVER raises —
    a discovery failure degrades to empty context rather than breaking the session."""
    try:
        root = find_project_root(cwd)
        return discover_instructions(cwd, root), environment_hints(cwd, root)
    except Exception:
        return (), {"cwd": str(cwd)}
