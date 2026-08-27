"""Tests for project-skill discovery in SkillStore (Fase 9, A2)."""

from pathlib import Path

from lohra.project.discover import discover_skill_roots
from lohra.skills.store import SkillStore


def _write_skill(root: Path, name: str, description: str, body: str = "do the thing") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n{body}\n",
        encoding="utf-8",
    )


def test_scans_project_skills_alongside_home(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / "skills", "home-one", "a home skill")
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "proj-one", "a project skill")
    store = SkillStore(home, extra_roots=(project,))
    names = {s.name for s in store.scan()}
    assert names == {"home-one", "proj-one"}


def test_index_labels_project_skills(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / "skills", "home-one", "h")
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "proj-one", "p")
    index = SkillStore(home, extra_roots=(project,)).index()
    assert "**proj-one** (project): p" in index
    assert "**home-one**: h" in index  # home skill unlabeled


def test_project_skill_wins_on_name_collision(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / "skills", "dup", "home version", body="HOME BODY")
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "dup", "project version", body="PROJECT BODY")
    store = SkillStore(home, extra_roots=(project,))
    skills = [s for s in store.scan() if s.name == "dup"]
    assert len(skills) == 1  # deduped
    got = store.get("dup")
    assert got.description == "project version" and got.body == "PROJECT BODY"


def test_symlinked_skill_md_is_skipped(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    secret = tmp_path / "secret.md"
    secret.write_text("---\nname: leak\ndescription: x\n---\nSECRET")
    sk = home / "skills" / "leak"
    sk.mkdir()
    (sk / "SKILL.md").symlink_to(secret)
    assert SkillStore(home).scan() == []  # symlinked SKILL.md not read


def test_create_still_targets_home(tmp_path):
    # A2: create/delete remain on the home root (A3 makes them project-aware).
    home = tmp_path / "home"
    project = tmp_path / "proj" / ".claude" / "skills"
    project.mkdir(parents=True)
    store = SkillStore(home, extra_roots=(project,))
    store.create("new-skill", "desc", "body")
    assert (home / "skills" / "new-skill" / "SKILL.md").exists()


def test_delete_never_touches_a_project_skill(tmp_path):
    # A2 invariant: delete is home-only — must NOT rmtree the user's project repo.
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "proj-only", "in the repo")
    store = SkillStore(home, extra_roots=(project,))
    assert store.delete("proj-only") is False  # not a home skill -> refused
    assert (project / "proj-only" / "SKILL.md").exists()  # repo file intact


def test_delete_collision_removes_home_copy_only(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / "skills", "dup", "home")
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "dup", "project")
    store = SkillStore(home, extra_roots=(project,))
    assert store.delete("dup") is True
    assert not (home / "skills" / "dup").exists()  # home copy removed
    assert (project / "dup" / "SKILL.md").exists()  # project copy untouched


def test_create_allowed_when_only_a_project_skill_shadows(tmp_path):
    # creating a home skill must work even if a project skill has the same name
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "shared", "project one")
    store = SkillStore(home, extra_roots=(project,))
    store.create("shared", "home one", "body")  # must not raise "already exists"
    assert (home / "skills" / "shared" / "SKILL.md").exists()


# --- A3: scope on create + in-place update ---


def test_create_with_project_scope_writes_to_project(tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "proj" / ".claude" / "skills"
    project.mkdir(parents=True)
    store = SkillStore(home, extra_roots=(project,))
    store.create("proj-skill", "d", "b", scope="project")
    assert (project / "proj-skill" / "SKILL.md").exists()
    assert not (home / "skills" / "proj-skill").exists()


def test_create_project_scope_without_project_errors(tmp_path):
    from lohra.skills.store import SkillValidationError

    store = SkillStore(tmp_path / "home")  # no project root
    try:
        store.create("x", "d", "b", scope="project")
        raised = False
    except SkillValidationError:
        raised = True
    assert raised


def test_update_edits_a_project_skill_in_place(tmp_path):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "p", "old desc", body="old body")
    store = SkillStore(home, extra_roots=(project,))
    updated = store.update("p", body="new body")
    assert updated.body == "new body"
    # written in place IN THE PROJECT, no copy leaked to home
    text = (project / "p" / "SKILL.md").read_text()
    assert "new body" in text
    assert not (home / "skills" / "p").exists()


def test_update_partial_keeps_other_fields(tmp_path):
    home = tmp_path / "home"
    _write_skill(home / "skills", "h", "keep this desc", body="orig")
    store = SkillStore(home)
    updated = store.update("h", body="changed")
    assert updated.description == "keep this desc" and updated.body == "changed"


def test_update_unknown_skill_errors(tmp_path):
    from lohra.skills.store import SkillValidationError

    store = SkillStore(tmp_path / "home")
    try:
        store.update("ghost", body="x")
        raised = False
    except SkillValidationError:
        raised = True
    assert raised


# --- A3 review fixes ---


def test_create_rejects_symlinked_dir_escape(tmp_path):
    # a hostile project plants .claude/skills/<name> -> outside; create must refuse
    from lohra.skills.store import SkillValidationError

    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    project = tmp_path / "proj" / ".claude" / "skills"
    project.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "evil").symlink_to(outside)  # symlinked dir named like a skill
    store = SkillStore(home, extra_roots=(project,))
    try:
        store.create("evil", "d", "b", scope="project")
        raised = False
    except SkillValidationError:
        raised = True
    assert raised
    assert not (outside / "SKILL.md").exists()  # no write escaped the project


def test_update_preserves_platforms(tmp_path):
    home = tmp_path / "home"
    sk = home / "skills" / "p"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: p\ndescription: d\nversion: 1.0.0\nplatforms: [linux, macos]\n---\nbody\n"
    )
    store = SkillStore(home)
    store.update("p", body="new body")
    text = (sk / "SKILL.md").read_text()
    assert "platforms" in text and "linux" in text  # not stripped on edit


def test_tool_rejects_noop_update(tmp_path):
    import json

    from lohra.skills.tool import SkillTool

    _write_skill(tmp_path / "skills", "s", "d")
    out = json.loads(SkillTool(SkillStore(tmp_path)).manage({"action": "update", "name": "s"}))
    assert "error" in out  # neither description nor body -> rejected, no write


def test_tool_create_symlink_to_file_returns_clean_error(tmp_path):
    # mkdir on a symlink-to-file raises OSError; manage must convert to a tool_error
    import json

    from lohra.skills.tool import SkillTool

    home = tmp_path / "home"
    skills = home / "skills"
    skills.mkdir(parents=True)
    target_file = tmp_path / "afile"
    target_file.write_text("x")
    (skills / "clash").symlink_to(target_file)  # name collides with a symlink-to-file
    out = json.loads(SkillTool(SkillStore(home)).manage(
        {"action": "create", "name": "clash", "body": "b", "description": "d"}
    ))
    assert "error" in out  # clean tool_error, not an uncaught raise


# --- discover_skill_roots ---


def test_discover_skill_roots_finds_project_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".lohra" / "skills").mkdir(parents=True)
    roots = discover_skill_roots(tmp_path)
    assert (tmp_path / ".claude" / "skills").resolve() in {r.resolve() for r in roots}
    assert (tmp_path / ".lohra" / "skills").resolve() in {r.resolve() for r in roots}


def test_discover_skill_roots_empty_when_none(tmp_path):
    (tmp_path / ".git").mkdir()
    assert discover_skill_roots(tmp_path) == ()


def test_discover_skill_roots_finds_from_subdir(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    sub = tmp_path / "pkg"
    sub.mkdir()
    roots = discover_skill_roots(sub)  # walks up to the VCS root
    assert len(roots) == 1
