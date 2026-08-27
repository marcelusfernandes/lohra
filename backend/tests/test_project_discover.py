"""Tests for project root + instruction discovery (Fase 9, A1)."""

from pathlib import Path

from lohra.project.discover import (
    discover_instructions,
    environment_hints,
    find_project_root,
    load_project_context,
)


# --- find_project_root ---


def test_root_is_dir_with_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_project_root(tmp_path) == tmp_path.resolve()


def test_root_walks_up_to_marker(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == tmp_path.resolve()


def test_no_marker_returns_start(tmp_path):
    nested = tmp_path / "x"
    nested.mkdir()
    assert find_project_root(nested) == nested.resolve()


def test_pyproject_is_a_marker(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert find_project_root(tmp_path) == tmp_path.resolve()


# --- discover_instructions ---


def test_reads_agents_and_claude_agents_first(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("agents rules")
    (tmp_path / "CLAUDE.md").write_text("claude rules")
    found = discover_instructions(tmp_path)
    assert [label for label, _ in found] == ["AGENTS.md", "CLAUDE.md"]
    assert found[0][1] == "agents rules"


def test_reads_single_file(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "CLAUDE.md").write_text("only claude")
    found = discover_instructions(tmp_path)
    assert len(found) == 1 and found[0][1] == "only claude"


def test_no_instructions_is_empty(tmp_path):
    (tmp_path / ".git").mkdir()
    assert discover_instructions(tmp_path) == ()


def test_walks_up_for_instructions(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("root agents")
    nested = tmp_path / "pkg" / "sub"
    nested.mkdir(parents=True)
    found = discover_instructions(nested)
    assert len(found) == 1 and found[0][1] == "root agents"


def test_nearest_file_wins(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("root")
    nested = tmp_path / "pkg"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("nested")
    found = discover_instructions(nested)
    assert len(found) == 1 and found[0][1] == "nested"  # nearest, only once


def test_large_file_is_truncated(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("x" * 40_000)
    content = discover_instructions(tmp_path)[0][1]
    assert "[...truncated]" in content and len(content) < 40_000


# --- environment_hints + load_project_context ---


def test_environment_hints(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a"
    nested.mkdir()
    hints = environment_hints(nested)
    assert hints["cwd"] == str(nested.resolve())
    assert hints["project_root"] == str(tmp_path.resolve())


def test_load_project_context_returns_both(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("rules")
    context, hints = load_project_context(tmp_path)
    assert context[0][1] == "rules"
    assert hints["project_root"] == str(tmp_path.resolve())


def test_monorepo_build_manifest_does_not_shadow_repo_instructions(tmp_path):
    # The dogfood `cd backend` case: outer repo has .git + CLAUDE.md; the package
    # has pyproject.toml but no instructions. The repo CLAUDE.md MUST still load.
    (tmp_path / ".git").mkdir()
    (tmp_path / "CLAUDE.md").write_text("repo rules")
    pkg = tmp_path / "backend"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("[project]")
    assert find_project_root(pkg) == tmp_path.resolve()  # VCS root, not the package
    found = discover_instructions(pkg)
    assert len(found) == 1 and found[0][1] == "repo rules"


def test_vcs_root_preferred_but_nearest_instruction_still_wins(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "CLAUDE.md").write_text("repo")
    pkg = tmp_path / "backend"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("[project]")
    (pkg / "CLAUDE.md").write_text("package")
    found = discover_instructions(pkg)
    assert len(found) == 1 and found[0][1] == "package"  # nearest beats the repo's


def test_symlinked_instruction_is_rejected(tmp_path):
    # security: a symlinked AGENTS.md -> a secret must NOT be read into the prompt
    (tmp_path / ".git").mkdir()
    secret = tmp_path / "secret.env"
    secret.write_text("ANTHROPIC_API_KEY=sk-leak")
    (tmp_path / "AGENTS.md").symlink_to(secret)
    assert discover_instructions(tmp_path) == ()  # symlink skipped, no exfil


def test_bounded_read_does_not_load_whole_giant_file(tmp_path):
    # the read is byte-capped: a large file is truncated, not fully loaded
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("y" * 500_000)
    content = discover_instructions(tmp_path)[0][1]
    assert "[...truncated]" in content and len(content) < 50_000


def test_non_regular_file_is_skipped(tmp_path):
    # a directory named AGENTS.md (not a regular file) is skipped, never crashes
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").mkdir()
    assert discover_instructions(tmp_path) == ()


def test_never_raises_on_missing_dir():
    # a cwd that doesn't exist must not crash discovery
    ghost = Path("/nonexistent/lohra/ghost/dir")
    context, hints = load_project_context(ghost)
    assert context == () and "cwd" in hints
