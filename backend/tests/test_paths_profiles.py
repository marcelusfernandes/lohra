"""Tests for profile-isolated workspaces (lohra.memory.paths)."""

import pytest

from lohra.memory import paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    return tmp_path


# --- validation (the security boundary) ---


@pytest.mark.parametrize("name", ["work", "client-a", "test_1", "A", "x" * 64])
def test_valid_names_pass(name):
    assert paths.validate_profile_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "..", ".", "foo/bar", "/etc", "..\\foo", "a b", "x" * 65, "-leading", "_under"],
)
def test_invalid_names_rejected(name):
    with pytest.raises(ValueError):
        paths.validate_profile_name(name)


# --- backward compatibility (the must-hold invariant) ---


def test_no_profile_means_base_home(_isolate):
    assert paths.active_profile() is None
    assert paths.lohra_home() == paths.lohra_base() == _isolate


# --- isolation (the integration assertion) ---


def test_profile_reroots_every_subsystem(monkeypatch, _isolate):
    monkeypatch.setenv("LOHRA_PROFILE", "work")
    expected_root = _isolate / "profiles" / "work"

    assert paths.active_profile() == "work"
    assert paths.lohra_home() == expected_root
    # all five state locations land under the profile dir
    assert paths.state_db_path() == expected_root / "state.db"
    assert paths.soul_path() == expected_root / "SOUL.md"
    assert paths.mcp_config_path() == expected_root / "mcp.json"
    assert (paths.lohra_home() / "memories").is_dir()
    assert (paths.lohra_home() / "skills").is_dir()
    assert (paths.lohra_home() / "cron").is_dir()
    # generated images route through lohra_home() too (see cli wiring)
    assert paths.lohra_home() / "images" == expected_root / "images"


def test_two_profiles_do_not_share_a_home(monkeypatch, _isolate):
    monkeypatch.setenv("LOHRA_PROFILE", "alice")
    alice = paths.lohra_home()
    monkeypatch.setenv("LOHRA_PROFILE", "bob")
    bob = paths.lohra_home()
    assert alice != bob
    assert alice.parent == bob.parent == _isolate / "profiles"


def test_out_of_band_invalid_profile_raises(monkeypatch):
    monkeypatch.setenv("LOHRA_PROFILE", "../escape")
    with pytest.raises(ValueError):
        paths.active_profile()


# --- listing ---


def test_list_profiles_empty_then_populated(monkeypatch, _isolate):
    assert paths.list_profiles() == []
    for name in ("zeta", "alpha"):
        monkeypatch.setenv("LOHRA_PROFILE", name)
        paths.ensure_home()
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    assert paths.list_profiles() == ["alpha", "zeta"]  # sorted
