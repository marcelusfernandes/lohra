"""SkillStore — procedural memory as SKILL.md files (spec §4).

Each skill is a directory under HOME/skills/<[category/]name>/ holding a
SKILL.md with YAML frontmatter (name, description, version, platforms) + a
markdown body. Progressive disclosure: the system-prompt index carries only
name + description; the full body loads on demand via skill_view.

Like memory, the index is frozen per session (load_snapshot) so mid-session
skill creation updates disk but not the live prompt (Invariante #1).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from lohra.safeio import read_text_bounded

# A single SKILL.md read is bounded (project skills are untrusted): no OOM/hang/
# symlink-exfil. Generous — skill bodies are instructions, not data dumps.
_MAX_SKILL_BYTES = 256_000

# How each tier is labelled in the progressive-disclosure index (home = unlabelled).
_LABELS = {"home": "", "project": " (project)", "builtin": " (builtin)"}

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESCRIPTION_LIMIT = 1024
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def builtin_root() -> Path:
    """The skills SHIPPED with Lohra (package data, read-only at runtime).

    Resolved off this module's own location, never the CWD — the frozen bundle
    and a site-packages install both put it next to the package."""
    return Path(__file__).resolve().parent / "builtin"


class SkillError(Exception):
    """Base for skill failures."""


class SkillFormatError(SkillError):
    """SKILL.md is missing or has malformed frontmatter."""


class SkillValidationError(SkillError):
    """A skill name/description is invalid, or the skill already exists."""


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    body: str
    path: Path | None = None
    platforms: tuple[str, ...] = ()


def parse_skill_md(content: str, path: Path | None) -> Skill:
    match = _FRONTMATTER.match(content)
    if not match:
        raise SkillFormatError("SKILL.md must start with a YAML frontmatter block (--- ... ---)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillFormatError(f"invalid SKILL.md frontmatter: {exc}") from exc
    if not isinstance(meta, dict) or not meta.get("name"):
        raise SkillFormatError("SKILL.md frontmatter must define a 'name'")
    platforms = meta.get("platforms") or ()
    return Skill(
        name=str(meta["name"]),
        description=str(meta.get("description", "")),
        version=str(meta.get("version", "")),
        body=match.group(2).strip(),
        path=path,
        platforms=tuple(platforms),
    )


def render_skill_md(
    name: str, description: str, body: str, version: str, platforms: tuple[str, ...] = ()
) -> str:
    data: dict = {"name": name, "description": description, "version": version}
    if platforms:  # round-trip the field so an update doesn't strip it
        data["platforms"] = list(platforms)
    front = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n{body.strip()}\n"


class SkillStore:
    def __init__(
        self,
        home: Path,
        extra_roots: tuple[Path, ...] = (),
        builtin_roots: tuple[Path, ...] = (),
    ) -> None:
        self.root = home / "skills"  # the writable (home) root
        # Precedence, highest first: project > home > builtin. Project roots come
        # from the repo the user is in; builtin roots ship with Lohra and are
        # scanned LAST, so a user/project copy of a builtin always wins and the
        # builtin never silently overrides something the user wrote.
        self._project_roots: tuple[Path, ...] = tuple(extra_roots)
        self._builtin_roots: tuple[Path, ...] = tuple(builtin_roots)
        self._roots: tuple[Path, ...] = (*self._project_roots, self.root, *self._builtin_roots)
        self._snapshot: str | None = None

    def _scan_root(self, root: Path) -> list[Skill]:
        """Parse every SKILL.md under ONE root (malformed/oversized/symlink skipped)."""
        out: list[Skill] = []
        if not root.exists():
            return out
        for skill_md in sorted(root.rglob("SKILL.md")):
            content = read_text_bounded(skill_md, _MAX_SKILL_BYTES)
            if content is None:
                continue  # symlink / non-regular / unreadable
            try:
                out.append(parse_skill_md(content, skill_md))
            except SkillFormatError:
                continue  # a malformed skill must not break the whole index
        return out

    def scan(self) -> list[Skill]:
        """Every skill across all roots, project first; first occurrence of a name
        wins (project precedence). For read/index — NOT for create/delete."""
        skills: list[Skill] = []
        seen: set[str] = set()
        for root in self._roots:
            for skill in self._scan_root(root):
                if skill.name in seen:
                    continue  # a higher-precedence root already provided it
                seen.add(skill.name)
                skills.append(skill)
        return skills

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.scan() if s.name == name), None)

    def _home_get(self, name: str) -> Skill | None:
        """Resolve a name in the HOME root ONLY — create/delete target home in A2
        and must never reach into (or destroy) an untrusted project skill dir."""
        return next((s for s in self._scan_root(self.root) if s.name == name), None)

    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _origin(self, skill: Skill) -> str:
        """Which tier a skill came from: 'home', 'builtin' or 'project'.

        Drives the index label AND the write guard — a builtin lives inside the
        installed package, so it must never be written in place."""
        if skill.path is None:
            return "home"
        if self._within(skill.path, self.root):
            return "home"
        if any(self._within(skill.path, root) for root in self._builtin_roots):
            return "builtin"
        return "project"

    def _write_root(self, scope: str) -> Path:
        """The root a create writes to: 'project' → the nearest project skill dir
        (must exist); anything else → home."""
        if scope == "project":
            project_roots = self._project_roots  # never positional: builtin roots trail
            if not project_roots:
                raise SkillValidationError(
                    "no project skill dir — run inside a project with .claude/skills"
                )
            return project_roots[0]
        return self.root

    def _under_roots(self, path: Path) -> bool:
        """Whether ``path`` resolves under a known root (guards symlinked-dir escape
        before an in-place write)."""
        resolved = path.resolve()
        for root in self._roots:
            try:
                resolved.relative_to(root.resolve())
                return True
            except (ValueError, OSError):
                continue
        return False

    def create(
        self, name: str, description: str, body: str, version: str = "1.0.0", scope: str = "home"
    ) -> Skill:
        if not NAME_PATTERN.match(name):
            raise SkillValidationError(
                f"invalid skill name {name!r}: use lowercase letters, digits, hyphens (≤64)"
            )
        if len(description) > DESCRIPTION_LIMIT:
            raise SkillValidationError(f"description over {DESCRIPTION_LIMIT} chars")
        target = self._write_root(scope)  # 'project' or home (A3); validates project exists
        if any(s.name == name for s in self._scan_root(target)):
            raise SkillValidationError(f"skill {name!r} already exists in this scope")
        path = target / name / "SKILL.md"
        if not self._under_roots(path):  # symmetry with update: no symlinked-dir escape
            raise SkillValidationError(f"refusing to write {name!r} outside known skill roots")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_skill_md(name, description, body, version), encoding="utf-8")
        return parse_skill_md(path.read_text(encoding="utf-8"), path)

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        body: str | None = None,
        version: str | None = None,
    ) -> Skill:
        """Edit a skill IN-PLACE where it lives (project precedence) — so editing a
        project skill writes to the project. Only fields given are changed.

        A BUILTIN skill is the exception: it lives inside the installed package
        (site-packages, or a frozen bundle) — read-only in practice, and wiped by
        the next install. Editing one COPIES IT ON WRITE into the home root and
        edits the copy, which then wins by precedence. The shipped file is never
        touched, and the agent's edit is what it sees from then on.
        """
        existing = self.get(name)  # precedence: edits the one the agent sees
        if existing is None or existing.path is None:
            raise SkillValidationError(f"no skill named {name!r}")
        if not self._under_roots(existing.path):
            raise SkillValidationError(f"skill {name!r} is outside known skill roots")
        new_desc = existing.description if description is None else description
        if len(new_desc) > DESCRIPTION_LIMIT:
            raise SkillValidationError(f"description over {DESCRIPTION_LIMIT} chars")
        new_body = existing.body if body is None else body
        new_version = existing.version or "1.0.0" if version is None else version
        rendered = render_skill_md(name, new_desc, new_body, new_version, existing.platforms)
        target = self._update_target(existing)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        return parse_skill_md(target.read_text(encoding="utf-8"), target)

    def _update_target(self, existing: Skill) -> Path:
        """Where an update writes: the skill's own file, or — for a builtin — a
        fresh home copy (copy-on-write, see ``update``)."""
        assert existing.path is not None
        if self._origin(existing) != "builtin":
            return existing.path
        copy = self.root / existing.name / "SKILL.md"
        if not self._under_roots(copy):  # symlinked home dir: refuse, don't escape
            raise SkillValidationError(
                f"refusing to write {existing.name!r} outside known skill roots"
            )
        return copy

    def delete(self, name: str) -> bool:
        # HOME-ONLY: never rmtree an untrusted project skill dir (A2 invariant).
        skill = self._home_get(name)
        if skill is None or skill.path is None:
            return False
        shutil.rmtree(skill.path.parent, ignore_errors=True)
        return True

    def index(self) -> str:
        """Progressive-disclosure block: name + description per skill, no bodies."""
        skills = self.scan()
        if not skills:
            return ""
        lines = [
            "## Skills (mandatory)",
            "Before answering, scan these. If one is relevant, load it with skill_view(name).",
            "",
        ]
        lines += [
            f"- **{s.name}**{_LABELS[self._origin(s)]}: {s.description}"
            for s in skills
        ]
        return "\n".join(lines)

    def load_snapshot(self) -> None:
        self._snapshot = self.index()

    def snapshot(self) -> str:
        if self._snapshot is None:
            self.load_snapshot()
        assert self._snapshot is not None
        return self._snapshot
