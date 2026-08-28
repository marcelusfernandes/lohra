"""Contract tests for the BUILTIN skill tier and `workflow-authoring` (M3.5).

Two things are pinned here, both invisible to the rest of the suite:

1. the builtin tier's precedence and write semantics — project > home > builtin,
   and an ``update`` of a builtin must never write into the installed package;
2. the skill's CONTENT against the code it documents — every executable node type
   is named, and every ```json fence in the body is a spec that passes the very
   validator ``WorkflowService.start`` applies. That is the anti-drift gate: the
   day someone changes the node surface without touching the skill, this fails.
"""

import json
import re
from pathlib import Path

import pytest

from lohra.skills.store import SkillStore, builtin_root
from lohra.skills.tool import SkillTool
from lohra.workflow.nodes import NODE_TYPES, WorkflowSpec
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import SUPPORTED_NODE_TYPES
from lohra.workflow.tools import RUN_GUIDANCE

SKILL_NAME = "workflow-authoring"
_JSON_FENCE = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def _store(home: Path, extra_roots: tuple[Path, ...] = ()) -> SkillStore:
    """A store wired exactly like equip.py wires the real one."""
    return SkillStore(home, extra_roots=extra_roots, builtin_roots=(builtin_root(),))


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def skill_body() -> str:
    store = _store(Path("/nonexistent-home"))
    skill = store.get(SKILL_NAME)
    assert skill is not None, "the builtin workflow-authoring skill must ship in the package"
    return skill.body


# --- (a) discovery + progressive disclosure --------------------------------


def test_builtin_skill_is_indexed(tmp_path):
    index = _store(tmp_path).index()
    assert f"**{SKILL_NAME}** (builtin):" in index


def test_builtin_body_loads_through_the_real_skill_view_path(tmp_path):
    # The agent never reads the file: it calls skill_view. Exercise THAT path.
    out = json.loads(SkillTool(_store(tmp_path)).view({"name": SKILL_NAME}))
    assert out.get("ok") is True
    assert out["name"] == SKILL_NAME
    assert len(out["body"]) > 2000  # a judgement skill, not a stub


# --- (b) precedence: project > home > builtin ------------------------------


def test_home_skill_overrides_the_builtin(tmp_path):
    _write_skill(tmp_path / "skills", SKILL_NAME, "my version", "HOME BODY")
    skill = _store(tmp_path).get(SKILL_NAME)
    assert skill.description == "my version" and skill.body == "HOME BODY"


def test_project_skill_overrides_home_and_builtin(tmp_path):
    _write_skill(tmp_path / "skills", SKILL_NAME, "home version", "HOME BODY")
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, SKILL_NAME, "project version", "PROJECT BODY")
    store = _store(tmp_path, extra_roots=(project,))
    skill = store.get(SKILL_NAME)
    assert skill.body == "PROJECT BODY"
    assert len([s for s in store.scan() if s.name == SKILL_NAME]) == 1  # deduped


def test_builtin_root_is_scanned_last(tmp_path):
    # Order matters for more than dedup: a project root must never be shadowed.
    project = tmp_path / "proj" / ".claude" / "skills"
    _write_skill(project, "proj-only", "p", "P")
    names = [s.name for s in _store(tmp_path, extra_roots=(project,)).scan()]
    assert names.index("proj-only") < names.index(SKILL_NAME)


# --- (c) write semantics on a builtin --------------------------------------


def test_update_of_a_builtin_copies_on_write_into_home(tmp_path):
    shipped = builtin_root() / SKILL_NAME / "SKILL.md"
    before = shipped.read_bytes()
    store = _store(tmp_path)
    out = json.loads(SkillTool(store).manage(
        {"action": "update", "name": SKILL_NAME, "body": "MY EDIT"}
    ))
    assert out.get("ok") is True
    assert shipped.read_bytes() == before  # the installed package is NEVER written
    copy = tmp_path / "skills" / SKILL_NAME / "SKILL.md"
    assert copy.exists()
    assert store.get(SKILL_NAME).body == "MY EDIT"  # the copy now wins


def test_copy_on_write_keeps_the_untouched_fields(tmp_path):
    shipped = _store(tmp_path).get(SKILL_NAME)
    updated = _store(tmp_path).update(SKILL_NAME, body="MY EDIT")
    assert updated.description == shipped.description  # description not clobbered
    assert updated.body == "MY EDIT"


def test_delete_refuses_a_builtin_skill(tmp_path):
    store = _store(tmp_path)
    assert store.delete(SKILL_NAME) is False
    assert (builtin_root() / SKILL_NAME / "SKILL.md").exists()
    out = json.loads(SkillTool(store).manage({"action": "delete", "name": SKILL_NAME}))
    assert "error" in out


def test_project_scope_create_still_needs_a_project_root(tmp_path):
    # The builtin root is appended LAST; a store that resolves project roots
    # positionally would hand a 'project' create the home (or builtin) dir.
    from lohra.skills.store import SkillValidationError

    with pytest.raises(SkillValidationError):
        _store(tmp_path).create("x", "d", "b", scope="project")


def test_project_scope_create_targets_the_project_root(tmp_path):
    project = tmp_path / "proj" / ".claude" / "skills"
    project.mkdir(parents=True)
    _store(tmp_path, extra_roots=(project,)).create("x", "d", "b", scope="project")
    assert (project / "x" / "SKILL.md").exists()


# --- (d) content contract: the node surface --------------------------------


def test_skill_names_every_node_type(skill_body):
    # Backticked, not merely present as a word: "agent" and "workflow" occur all
    # over the prose, so a bare substring check would pass on a skill that never
    # actually documents the type.
    for node_type in sorted(NODE_TYPES):
        assert f"`{node_type}`" in skill_body, f"{node_type} undocumented in the skill"


def test_run_guidance_names_every_node_type():
    for node_type in sorted(NODE_TYPES):
        assert node_type in RUN_GUIDANCE, f"{node_type} undocumented in RUN_GUIDANCE"


def test_run_guidance_points_at_the_skill_by_its_real_name():
    assert SKILL_NAME in RUN_GUIDANCE
    assert "resume_run_id" in RUN_GUIDANCE and "paused" in RUN_GUIDANCE


# --- (e) content contract: every example spec still validates ---------------


def test_every_json_fence_is_a_valid_spec(skill_body):
    fences = _JSON_FENCE.findall(skill_body)
    # Guard the guard: a regex that matches nothing would make this test a no-op.
    assert len(fences) >= 5, f"expected >=5 example specs, found {len(fences)}"
    for index, raw in enumerate(fences):
        spec = json.loads(raw)  # must be real JSON, not pseudo-JSON
        parsed = validate_spec(spec, supported_types=SUPPORTED_NODE_TYPES)
        assert isinstance(parsed, WorkflowSpec), (
            f"example #{index} does not validate:\n{parsed.message}"
        )
