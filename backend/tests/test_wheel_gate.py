"""Hermetic gate controls use pure fixtures; no build, pip install or inference."""

import json

import pytest

from ci.installed_smoke import check_no_provider, verify_installed_files, verify_origin
from ci.wheel_gate import Gate, clean_environment, provenance


def test_environment_drops_provider_profile_and_python_leaks_preserving_homes():
    env = clean_environment({"HOME": "/home/owner", "CODEX_HOME": "/home/codex",
                             "PATH": "/bin", "OPENAI_API_KEY": "fake",
                             "LOHRA_PROFILE": "personal", "PYTHONPATH": "/checkout",
                             "PIP_INDEX_URL": "https://user:fake@example.invalid"})
    assert env["HOME"] == "/home/owner" and env["CODEX_HOME"] == "/home/codex"
    assert not {"OPENAI_API_KEY", "LOHRA_PROFILE", "PYTHONPATH", "PIP_INDEX_URL"} & env.keys()


def test_pr_head_and_base_do_not_replace_checkout_identity():
    result = provenance("merge-sha", "tree-sha", {"pull_request": {
        "head": {"sha": "head-sha"}, "base": {"sha": "base-sha"}}})
    assert result == {"source_sha": "merge-sha", "source_tree": "tree-sha",
                      "pr_head_sha": "head-sha", "pr_base_sha": "base-sha"}
    assert provenance("main", "tree", {})["pr_head_sha"] is None


def test_wrong_import_origin_cannot_pass_even_with_matching_version(tmp_path):
    purelib = tmp_path / "venv/site-packages"
    verify_origin(purelib / "lohra/__init__.py", purelib)
    with pytest.raises(ValueError, match="outside"):
        verify_origin(tmp_path / "checkout/lohra/__init__.py", purelib)


@pytest.mark.parametrize("case", ["missing", "changed"])
def test_installed_package_bytes_are_checked_against_source(tmp_path, case):
    import hashlib

    path = tmp_path / "lohra/module.py"
    path.parent.mkdir()
    wanted = {"lohra/module.py": hashlib.sha256(b"original").hexdigest()}
    if case == "changed":
        path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="installed package"):
        verify_installed_files(tmp_path, wanted)


def test_installer_generated_files_do_not_break_expected_content_checks(tmp_path):
    import hashlib

    package = tmp_path / "lohra"
    package.mkdir()
    (package / "module.py").write_bytes(b"original")
    (package / "module.pyc").write_bytes(b"generated")
    (tmp_path / "INSTALLER").write_text("pip")
    verify_installed_files(tmp_path, {"lohra/module.py": hashlib.sha256(b"original").hexdigest()})


@pytest.fixture
def no_provider():
    return {"completed": False, "api_calls": 0, "output": None, "tool_calls": [],
            "error": "no provider configured — run `lohra init`"}


def test_precise_unconfigured_json_and_exit_are_accepted(no_provider):
    check_no_provider(2, json.dumps(no_provider))


@pytest.mark.parametrize(("code", "patch"), [
    (1, {}), (0, {}), (2, {"error": "missing module"}), (2, {"completed": True}),
    (2, {"api_calls": 1}), (2, {"output": "answer"}), (2, {"tool_calls": [{}]}),
])
def test_arbitrary_failure_is_not_an_unconfigured_smoke_pass(no_provider, code, patch):
    with pytest.raises(ValueError, match="unconfigured"):
        check_no_provider(code, json.dumps({**no_provider, **patch}))


def test_non_json_failure_is_rejected():
    with pytest.raises(ValueError, match="unconfigured"):
        check_no_provider(2, "ImportError")


def test_failing_stage_keeps_its_identity_error_and_duration(tmp_path):
    gate = Gate(tmp_path)

    def broken():
        raise ValueError("synthetic install failure")

    with pytest.raises(ValueError, match="synthetic install"):
        gate.stage("install", broken)
    report = json.loads((tmp_path / "result.json").read_text())
    phase, = report["phases"]
    assert phase["name"] == "install" and phase["status"] == "failed"
    assert phase["seconds"] >= 0 and "synthetic install failure" in phase["error"]


def test_nonzero_subprocess_keeps_exit_and_log_without_passing_stage(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 7, "build out\n", "build error\n"))
    gate = Gate(tmp_path)
    with pytest.raises(RuntimeError, match="exited 7"):
        gate.command("build", ["synthetic-builder"], tmp_path, {})
    phase, = json.loads((tmp_path / "result.json").read_text())["phases"]
    assert phase["status"] == "failed" and phase["exit_code"] == 7
    assert (tmp_path / "build.log").read_text() == "build out\nbuild error\n"


def test_timeout_keeps_partial_output_and_failed_phase(tmp_path, monkeypatch):
    import subprocess

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 3, output=b"partial", stderr=b"timeout")

    monkeypatch.setattr(subprocess, "run", timeout)
    gate = Gate(tmp_path)
    with pytest.raises(RuntimeError, match="timed out"):
        gate.command("install", ["synthetic-installer"], tmp_path, {}, timeout=3)
    phase, = json.loads((tmp_path / "result.json").read_text())["phases"]
    assert phase["status"] == "failed" and "partial" in (tmp_path / "install.log").read_text()
