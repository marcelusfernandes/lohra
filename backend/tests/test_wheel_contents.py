"""Wheel completeness is derived from tracked source, not its own RECORD."""

import base64
import csv
import hashlib
import io
import zipfile

import pytest

from ci.wheel_contents import inventory_from_paths, verify_wheel


def make_wheel(path, contents, *, version="0.0.27"):
    files = {**contents, f"lohra-{version}.dist-info/METADATA":
             f"Metadata-Version: 2.1\nName: lohra\nVersion: {version}\n".encode()}
    record = f"lohra-{version}.dist-info/RECORD"
    stream = io.StringIO()
    writer = csv.writer(stream)
    for name, value in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + digest, len(value)))
    writer.writerow((record, "", ""))
    files[record] = stream.getvalue().encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return path


@pytest.fixture
def source(tmp_path):
    contents = {
        "lohra/__init__.py": b'__version__ = "0.0.27"\n',
        "lohra/nested/new_module.py": b"VALUE = 42\n",
        "lohra/skills/builtin/example/SKILL.md": b"builtin body\n",
        "lohra/skills/export/example/SKILL.md": b"export body\n",
        "lohra/new_asset.json": b'{"new": true}\n',
    }
    for name, value in contents.items():
        path = tmp_path / "backend" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    paths = ["backend/" + name for name in contents]
    return contents, inventory_from_paths(tmp_path, paths)


def test_all_tracked_modules_and_assets_are_expected_without_a_manifest(tmp_path, source):
    contents, expected = source
    assert set(expected) == set(contents)
    result = verify_wheel(make_wheel(tmp_path / "valid.whl", contents), expected, "0.0.27")
    assert result["package_files"] == 5 and result["record_entries"] == 7


@pytest.mark.parametrize("missing", [
    "lohra/nested/new_module.py", "lohra/skills/builtin/example/SKILL.md",
    "lohra/skills/export/example/SKILL.md", "lohra/new_asset.json",
])
def test_record_consistent_omission_still_fails_completeness(tmp_path, source, missing):
    contents, expected = source
    # make_wheel generates a correct RECORD for the already-incomplete contents.
    wheel = make_wheel(tmp_path / "missing.whl", {k: v for k, v in contents.items() if k != missing})
    with pytest.raises(ValueError, match="missing package files") as error:
        verify_wheel(wheel, expected, "0.0.27")
    assert missing in str(error.value)


def test_changed_module_with_valid_record_and_same_version_is_not_current_source(tmp_path, source):
    contents, expected = source
    changed = {**contents, "lohra/nested/new_module.py": b"VALUE = 0\n"}
    wheel = make_wheel(tmp_path / "stale.whl", changed)
    with pytest.raises(ValueError, match="package bytes differ"):
        verify_wheel(wheel, expected, "0.0.27")


def test_untracked_file_does_not_enter_source_expectations(tmp_path, source):
    contents, _ = source
    (tmp_path / "backend/lohra/local.py").write_text("LOCAL = True")
    actual = inventory_from_paths(tmp_path, ["backend/" + name for name in contents])
    assert "lohra/local.py" not in actual


def test_empty_inventory_cannot_pass_vacuously(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        inventory_from_paths(tmp_path, [])
    with pytest.raises(ValueError, match="empty"):
        verify_wheel(make_wheel(tmp_path / "empty.whl", {}), {}, "0.0.27")


def test_record_hash_corruption_is_distinct_from_source_completeness(tmp_path, source):
    contents, expected = source
    wheel = make_wheel(tmp_path / "corrupt.whl", contents)
    with zipfile.ZipFile(wheel) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    files["lohra/nested/new_module.py"] = b"VALUE = -1\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    with pytest.raises(ValueError, match="RECORD hash/size"):
        verify_wheel(wheel, expected, "0.0.27")


def test_distribution_version_must_match_source(tmp_path, source):
    contents, expected = source
    with pytest.raises(ValueError, match="version"):
        verify_wheel(make_wheel(tmp_path / "other.whl", contents, version="9.0"), expected, "0.0.27")


def test_source_path_must_remain_in_package(tmp_path):
    with pytest.raises(ValueError, match="package path"):
        inventory_from_paths(tmp_path, ["backend/elsewhere.py"])


def test_unexpected_package_file_is_visible(tmp_path, source):
    contents, expected = source
    with pytest.raises(ValueError, match="unexpected package files"):
        verify_wheel(make_wheel(tmp_path / "extra.whl", {**contents, "lohra/extra.py": b""}),
                     expected, "0.0.27")
