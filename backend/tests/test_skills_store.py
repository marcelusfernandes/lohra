"""Tests for SkillStore — SKILL.md parsing, scan, progressive-disclosure index (§4)."""

import pytest

from lohra.skills.store import (
    SkillFormatError,
    SkillStore,
    SkillValidationError,
    parse_skill_md,
)

SAMPLE = """---
name: deploy-backend
description: Deploy the Lohra backend to production with health checks.
version: 1.2.0
platforms: [macos, linux]
---
# Deploy Backend

1. Run the test suite.
2. Build and push.
"""


def test_parse_skill_md():
    skill = parse_skill_md(SAMPLE, path=None)
    assert skill.name == "deploy-backend"
    assert "Deploy the Lohra backend" in skill.description
    assert skill.version == "1.2.0"
    assert skill.platforms == ("macos", "linux")
    assert "Run the test suite" in skill.body


def test_parse_missing_frontmatter_raises():
    with pytest.raises(SkillFormatError):
        parse_skill_md("# just a body, no frontmatter", path=None)


def test_parse_missing_name_raises():
    with pytest.raises(SkillFormatError):
        parse_skill_md("---\ndescription: x\n---\nbody", path=None)


@pytest.fixture
def store(tmp_path):
    return SkillStore(tmp_path)


def test_create_and_get(store):
    store.create("greet-user", "Say hi nicely.", "# Greet\nSay hello.")
    skill = store.get("greet-user")
    assert skill is not None
    assert skill.description == "Say hi nicely."
    assert "Say hello." in skill.body


def test_create_writes_skill_md(store):
    store.create("greet-user", "desc", "# body")
    assert (store.root / "greet-user" / "SKILL.md").exists()


def test_create_invalid_name_raises(store):
    with pytest.raises(SkillValidationError):
        store.create("Bad Name!", "desc", "body")  # spaces + uppercase + punct


def test_create_duplicate_raises(store):
    store.create("dup", "desc", "body")
    with pytest.raises(SkillValidationError):
        store.create("dup", "desc2", "body2")


def test_delete(store):
    store.create("temp", "desc", "body")
    assert store.delete("temp") is True
    assert store.get("temp") is None


def test_scan_finds_multiple(store):
    store.create("alpha", "first skill", "body a")
    store.create("beta", "second skill", "body b")
    names = {s.name for s in store.scan()}
    assert names == {"alpha", "beta"}


def test_index_is_metadata_only(store):
    store.create("alpha", "what alpha does", "SECRET BODY CONTENT")
    index = store.index()
    assert "alpha" in index
    assert "what alpha does" in index
    assert "SECRET BODY CONTENT" not in index  # progressive disclosure: no bodies


def test_index_empty_when_no_skills(store):
    assert store.index() == ""


# --- frozen snapshot ---


def test_snapshot_frozen_until_reload(store):
    store.create("one", "first", "body")
    store.load_snapshot()
    snap = store.snapshot()
    store.create("two", "second", "body")
    assert store.snapshot() == snap
    assert "two" not in store.snapshot()


def test_snapshot_refreshes_on_reload(store):
    store.create("one", "first", "body")
    store.load_snapshot()
    store.create("two", "second", "body")
    store.load_snapshot()
    assert "two" in store.snapshot()
