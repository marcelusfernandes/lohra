"""A4: project instructions + skills reach the frozen system prompt (Fase 9).

Locks the mechanics the live E2E proved (Lohra in a project followed AGENTS.md
and used a project skill): the discovered AGENTS.md/CLAUDE.md and the project
skill index both land in the agent's frozen system prompt — no LLM needed.
"""

from lohra.agent.system_prompt import build_system_prompt
from lohra.project.discover import discover_skill_roots, load_project_context
from lohra.skills.store import SkillStore


def _write_skill(root, name, description, body="do it"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1.0.0\n---\n{body}\n"
    )


def test_project_instructions_and_skills_in_frozen_prompt(tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "AGENTS.md").write_text("PROJECT RULE: always be terse.")
    _write_skill(project / ".claude" / "skills", "deploy-helper", "how to deploy this repo")

    home = tmp_path / "home"  # the agent's own (separate) home skills
    _write_skill(home / "skills", "home-skill", "a personal skill")

    context_files, env_hints = load_project_context(project)
    store = SkillStore(home, extra_roots=discover_skill_roots(project))

    snapshot = build_system_prompt(
        context_files=context_files,
        environment_hints=env_hints,
        skills_index=store.snapshot(),
    )
    text = snapshot.text
    # A1: the project instruction is in the frozen prompt
    assert "PROJECT RULE: always be terse." in text
    assert str(project.resolve()) in text  # project_root env hint
    # A2: the project skill is in the index, labelled, alongside the home skill
    assert "deploy-helper" in text and "(project)" in text
    assert "home-skill" in text
