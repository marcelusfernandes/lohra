"""#98 platform-index preparation: real SkillStore, synthetic roots and OS.

Only platform.system/sys.platform are simulated; no production filter is patched.
Omitting platforms means unrestricted. No wildcard platform syntax is introduced.
"""

import json
import os
from pathlib import Path
import platform
import re
import socket
import sys

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.skills import store as module
from lohra.skills.store import SkillStore, render_skill_md
from lohra.skills.tool import SkillTool

SYSTEMS = {
    "macos": ("Darwin", "darwin"),
    "linux": ("Linux", "linux"),
    "windows": ("Windows", "win32"),
}
RESTRICTIONS = {
    "portable": (), "mac-only": ("macos",), "linux-only": ("linux",),
    "windows-only": ("windows",), "unix-pair": ("macos", "linux"),
}
EXPECTED = {
    "macos": {"portable", "mac-only", "unix-pair"},
    "linux": {"portable", "linux-only", "unix-pair"},
    "windows": {"portable", "windows-only"},
}


class NoInference(ModelClient):
    def create(self, **kwargs):
        raise AssertionError("skill index tests do not call a provider")


def put(root, name, platforms=(), description="fixture", body="PRIVATE BODY"):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_skill_md(name, description, body, "1.0.0", platforms), encoding="utf-8")
    return path


def names(index):
    return set(re.findall(r"^- \*\*([^*]+)\*\*", index, re.M))


@pytest.fixture(autouse=True)
def isolated_skills(tmp_path, monkeypatch):
    expected = os.environ.get("LOHRA98_EXPECTED_BACKEND")
    if expected:
        assert Path(module.__file__).resolve() == Path(expected) / "lohra/skills/store.py"
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)

    def denied(*args, **kwargs):
        raise AssertionError("no network in skill metadata tests")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture(params=tuple(SYSTEMS))
def operating_system(request, monkeypatch):
    native, sysname = SYSTEMS[request.param]
    monkeypatch.setattr(platform, "system", lambda: native)
    monkeypatch.setattr(sys, "platform", sysname)
    return request.param


def test_index_filters_platforms_but_discovery_and_explicit_view_stay_complete(tmp_path, operating_system):
    for name, platforms in RESTRICTIONS.items():
        put(tmp_path / "skills", name, platforms)
    store = SkillStore(tmp_path)
    index = store.index()
    assert "PRIVATE BODY" not in index
    assert {skill.name: skill.platforms for skill in store.scan()} == RESTRICTIONS
    assert store.get("windows-only").platforms == ("windows",)
    viewed = json.loads(SkillTool(store).view({"name": "windows-only"}))
    assert viewed["ok"] and viewed["body"] == "PRIVATE BODY"
    assert names(index) == EXPECTED[operating_system]


def test_all_incompatible_skills_leave_no_mandatory_header(tmp_path, operating_system):
    others = [key for key in SYSTEMS if key != operating_system]
    for key in others:
        put(tmp_path / "skills", key + "-only", (key,))
    store = SkillStore(tmp_path)
    assert len(store.scan()) == 2
    assert store.index() == ""


def test_filter_runs_after_precedence_without_falling_back_to_home_or_builtin(tmp_path, operating_system):
    other = next(key for key in SYSTEMS if key != operating_system)
    home, project, builtin = tmp_path / "home", tmp_path / "project", tmp_path / "builtin"
    lower = [put(home / "skills", "shadowed", description="HOME"),
             put(builtin, "shadowed", description="BUILTIN")]
    original = [path.read_bytes() for path in lower]
    selected = put(project, "shadowed", (other,), description="PROJECT", body="PROJECT BODY")
    put(home / "skills", "portable")
    store = SkillStore(home, extra_roots=(project,), builtin_roots=(builtin,))
    assert store.get("shadowed").path == selected
    assert [skill.name for skill in store.scan()].count("shadowed") == 1
    viewed = json.loads(SkillTool(store).view({"name": "shadowed"}))
    assert viewed["ok"] and viewed["body"] == "PROJECT BODY"
    assert store.get("shadowed").description == "PROJECT"
    updated = store.update("shadowed", description="PROJECT UPDATED", body="UPDATED BODY")
    assert updated.path == selected and updated.platforms == (other,)
    assert updated.version == "1.0.0" and store.get("shadowed").body == "UPDATED BODY"
    assert [path.read_bytes() for path in lower] == original
    assert names(store.index()) == {"portable"}  # selected project name is hidden, not replaced


def test_updates_preserve_platforms_and_frozen_store_and_agent_snapshots(tmp_path, operating_system):
    put(tmp_path / "skills", "portable", description="before-update")
    restricted = put(tmp_path / "skills", "current-os", (operating_system,))
    store = SkillStore(tmp_path)
    item = Agent(model="synthetic", provider=get_provider_profile("anthropic"),
                 client=NoInference(), skill_store=store)
    prompt, frozen = item.system_prompt().text, store.snapshot()
    assert names(frozen) == {"portable", "current-os"}
    assert "PRIVATE BODY" not in prompt
    store.update("portable", description="after-update")
    changed = store.update("current-os", body="UPDATED BODY")
    assert changed.path == restricted and changed.platforms == (operating_system,)
    assert changed.version == "1.0.0"
    assert json.loads(SkillTool(store).view({"name": "current-os"}))["body"] == "UPDATED BODY"
    assert "after-update" in store.index() and "before-update" not in store.index()
    assert store.snapshot() == frozen and item.system_prompt().text == prompt
    fresh = SkillStore(tmp_path)
    assert "after-update" in fresh.snapshot() and fresh.snapshot() != frozen


def test_unknown_host_only_indexes_unrestricted_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")
    monkeypatch.setattr(sys, "platform", "freebsd13")
    put(tmp_path / "skills", "portable")
    for key in (*SYSTEMS, "freebsd"):
        put(tmp_path / "skills", key + "-only", (key,))
    store = SkillStore(tmp_path)
    assert len(store.scan()) == 5 and store.get("freebsd-only").platforms == ("freebsd",)
    assert names(store.index()) == {"portable"}
    assert store.delete("portable")
    assert store.index() == ""
