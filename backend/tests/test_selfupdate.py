"""Tests for self-update — every real-world outcome via a scripted git runner."""

from pathlib import Path

import pytest

from lohra.selfupdate import repo as gitrepo
from lohra.selfupdate import service


def _runner(responses):
    """Build a GitRunner that maps the git subcommand to a scripted reply.

    ``responses`` keys are matched as a prefix of the args list (joined by ' ').
    Each value is (returncode, stdout, stderr).
    """

    def run(args, cwd):
        key = " ".join(args)
        for prefix, reply in responses.items():
            if key.startswith(prefix):
                return reply
        raise AssertionError(f"unexpected git call: {key}")

    return run


_REPO = Path("/fake/repo")


# --- locate_repo ---


def test_locate_repo_walks_up_to_dot_git(tmp_path):
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "backend" / "lohra"
    nested.mkdir(parents=True)
    assert gitrepo.locate_repo(nested / "__init__.py") == tmp_path


def test_locate_repo_returns_none_without_git(tmp_path):
    assert gitrepo.locate_repo(tmp_path) is None


# --- perform_update outcomes ---


def test_dirty_tree_aborts():
    runner = _runner({"status --porcelain": (0, " M file.py", "")})
    result = service.perform_update(_REPO, runner)
    assert result.status == service.DIRTY
    assert not result.ok


def test_no_upstream_aborts():
    runner = _runner(
        {
            "status --porcelain": (0, "", ""),
            "symbolic-ref": (0, "feature-x", ""),
            "rev-parse --abbrev-ref": (1, "", "no upstream"),
        }
    )
    result = service.perform_update(_REPO, runner)
    assert result.status == service.NO_UPSTREAM


def test_detached_head_aborts():
    runner = _runner(
        {
            "status --porcelain": (0, "", ""),
            "symbolic-ref": (1, "", ""),  # detached
        }
    )
    result = service.perform_update(_REPO, runner)
    assert result.status == service.NO_UPSTREAM


def test_diverged_is_distinct_from_error():
    runner = _runner(
        {
            "status --porcelain": (0, "", ""),
            "symbolic-ref": (0, "main", ""),
            "rev-parse --abbrev-ref": (0, "origin/main", ""),
            "rev-parse HEAD": (0, "aaaaaaa", ""),
            "pull --ff-only": (1, "", "could not fast-forward"),
            # structural divergence check: HEAD is NOT an ancestor of @{u}
            "merge-base --is-ancestor": (1, "", ""),
        }
    )
    result = service.perform_update(_REPO, runner)
    assert result.status == service.DIVERGED


def test_pull_failure_that_is_not_divergence_is_error():
    runner = _runner(
        {
            "status --porcelain": (0, "", ""),
            "symbolic-ref": (0, "main", ""),
            "rev-parse --abbrev-ref": (0, "origin/main", ""),
            "rev-parse HEAD": (0, "aaaaaaa", ""),
            "pull --ff-only": (1, "", "some network error"),
            "merge-base --is-ancestor": (0, "", ""),  # HEAD still an ancestor → not diverged
        }
    )
    result = service.perform_update(_REPO, runner)
    assert result.status == service.ERROR


def test_already_up_to_date():
    runner = _runner(
        {
            "status --porcelain": (0, "", ""),
            "symbolic-ref": (0, "main", ""),
            "rev-parse --abbrev-ref": (0, "origin/main", ""),
            "rev-parse HEAD": (0, "samesha", ""),
            "pull --ff-only": (0, "Already up to date.", ""),
        }
    )
    result = service.perform_update(_REPO, runner)
    assert result.status == service.UP_TO_DATE
    assert result.ok


def test_successful_update_reports_files_and_restart():
    shas = iter(["oldsha1", "newsha2"])
    responses = {
        "status --porcelain": (0, "", ""),
        "symbolic-ref": (0, "main", ""),
        "rev-parse --abbrev-ref": (0, "origin/main", ""),
        "pull --ff-only": (0, "Updating oldsha1..newsha2", ""),
        "diff --name-only": (0, "lohra/cli.py\nlohra/agent/loop.py", ""),
    }

    def run(args, cwd):
        key = " ".join(args)
        if key == "rev-parse HEAD":
            return (0, next(shas), "")
        for prefix, reply in responses.items():
            if key.startswith(prefix):
                return reply
        raise AssertionError(key)

    result = service.perform_update(_REPO, run)
    assert result.status == service.UPDATED
    assert result.restart_required is True
    assert result.reinstall_recommended is False
    assert result.changed_files == ("lohra/cli.py", "lohra/agent/loop.py")


def test_pyproject_change_recommends_reinstall():
    shas = iter(["old", "new"])
    responses = {
        "status --porcelain": (0, "", ""),
        "symbolic-ref": (0, "main", ""),
        "rev-parse --abbrev-ref": (0, "origin/main", ""),
        "pull --ff-only": (0, "Updating", ""),
        "diff --name-only": (0, "backend/pyproject.toml", ""),
    }

    def run(args, cwd):
        key = " ".join(args)
        if key == "rev-parse HEAD":
            return (0, next(shas), "")
        for prefix, reply in responses.items():
            if key.startswith(prefix):
                return reply
        raise AssertionError(key)

    result = service.perform_update(_REPO, run)
    assert result.reinstall_recommended is True


# --- check_update ---


def test_check_reports_behind():
    runner = _runner(
        {
            "symbolic-ref": (0, "main", ""),
            "rev-parse --abbrev-ref": (0, "origin/main", ""),
            "fetch": (0, "", ""),
            "rev-list --count": (0, "3", ""),
        }
    )
    result = service.check_update(_REPO, runner)
    assert result.status == service.BEHIND
    assert "3" in result.message


def test_check_up_to_date():
    runner = _runner(
        {
            "symbolic-ref": (0, "main", ""),
            "rev-parse --abbrev-ref": (0, "origin/main", ""),
            "fetch": (0, "", ""),
            "rev-list --count": (0, "0", ""),
        }
    )
    result = service.check_update(_REPO, runner)
    assert result.status == service.UP_TO_DATE


def test_check_no_upstream():
    runner = _runner(
        {
            "symbolic-ref": (0, "feature", ""),
            "rev-parse --abbrev-ref": (1, "", "no upstream"),
        }
    )
    result = service.check_update(_REPO, runner)
    assert result.status == service.NO_UPSTREAM


# --- reinstall ---


def test_reinstall_invokes_pip_on_backend_dir():
    seen = {}

    def pip(backend_dir):
        seen["dir"] = backend_dir
        return True, "ok"

    ok, _ = service.reinstall(_REPO, pip)
    assert ok
    assert seen["dir"] == _REPO / "backend"


# --- CLI wiring ---


def test_cli_update_not_a_repo(monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setattr(service, "resolve_repo", lambda: None)
    assert cli.run_update() == 2
    assert "not a git checkout" in capsys.readouterr().err


def test_cli_update_check_path(monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setattr(service, "resolve_repo", lambda: _REPO)
    monkeypatch.setattr(
        service, "check_update", lambda repo: service.UpdateResult(service.BEHIND, "2 behind")
    )
    assert cli.run_update(check=True) == 0
    assert "2 behind" in capsys.readouterr().out


def test_cli_update_reinstall_runs_when_recommended(monkeypatch, capsys):
    from lohra import cli

    monkeypatch.setattr(service, "resolve_repo", lambda: _REPO)
    monkeypatch.setattr(
        service,
        "perform_update",
        lambda repo: service.UpdateResult(
            service.UPDATED, "updated", reinstall_recommended=True, restart_required=True
        ),
    )
    called = {}

    def fake_reinstall(repo):
        called["x"] = True
        return True, ""

    monkeypatch.setattr(service, "reinstall", fake_reinstall)
    assert cli.run_update(reinstall=True) == 0
    assert called.get("x") is True
    assert "restart" in capsys.readouterr().out


def test_cli_update_recommends_working_remedy_not_rerun(monkeypatch, capsys):
    # When deps changed but --reinstall wasn't passed, the hint must be the
    # remedy that works (reinstall current tree), never "re-run lohra update".
    from lohra import cli

    monkeypatch.setattr(service, "resolve_repo", lambda: _REPO)
    monkeypatch.setattr(
        service,
        "perform_update",
        lambda repo: service.UpdateResult(
            service.UPDATED, "updated", reinstall_recommended=True, restart_required=True
        ),
    )
    # reinstall must NOT run without the flag
    monkeypatch.setattr(
        service, "reinstall", lambda repo: pytest.fail("reinstall should not run")
    )
    assert cli.run_update(reinstall=False) == 0
    out = capsys.readouterr().out
    assert "pip install -e" in out
    assert "re-run" not in out.lower()
