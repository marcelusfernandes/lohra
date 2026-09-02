"""Wave 8 E2 (#42/#45) — honest text for the run's shared filesystem scope.

Investigation `docs/history/reviews/2026-09-02-wave8-investigation.md` (Report
2) found the quiescence fault's ``_ALIVE_HINT`` mis-addressed: it named
``working_root`` alone, but in the live run examined there the six ``work-N``
directories were EMPTY — the resource a still-alive leaf actually kept
mutating was the user's project, reached through ``terminal`` /
``fs_allow``, not the sandboxed scratch directory. The same run showed
`docs/specs/07-workflow-harness.md` §8.3 describing the working scope as
``~/.lohra/runs/<run_id>/work``, when the code (`service.py`) has named it
``work-{fence}`` (one directory per lease acquisition) since issue #12.

This is the anti-drift contract for both fixes, following the precedent of
``test_every_node_type_has_a_strategy_and_is_executable`` in
``tests/test_workflow_m7_fixes.py``: pin the STRING each surface says, not
just that a docstring somewhere was edited, so a future edit to one side
without the other fails a test instead of drifting silently again.
"""

import re
from pathlib import Path

import pytest

from lohra.state import SessionDB
from lohra.workflow.quiescence import _ALIVE_HINT
from lohra.workflow import service as service_module
from tests.test_workflow_durable_state import _TWO_NODE, _service

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "specs" / "07-workflow-harness.md"


@pytest.fixture
def db(tmp_path):
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _spec_section_8_3() -> str:
    text = SPEC_PATH.read_text(encoding="utf-8")
    match = re.search(r"### 8\.3 .*?(?=\n### 8\.4)", text, re.DOTALL)
    assert match is not None, "spec §8.3 header not found — section renamed/moved?"
    return match.group(0)


# --- a. the fault hint names the WHOLE scope, not just the sandbox dir -----


def test_the_alive_hint_names_the_whole_filesystem_scope():
    """A still-alive leaf can mutate more than its own ``working_root``: any
    operator-allowed ``fs_allow`` root, and (with a shell opted in) anything
    that reaches. The hint an author reads in the fault must say so, not just
    name the one directory that turned out empty in the run that found this."""
    assert "working_root" in _ALIVE_HINT
    assert "fs_allow" in _ALIVE_HINT


# --- b. the spec's working-scope path matches the code -----------------


def test_the_spec_names_the_real_working_root_path():
    """The scratch directory is per lease ACQUISITION (``work-{fence}``,
    issue #12), not per run (``work`` alone) — the spec drifted from the code
    that has been true since #12. Both the correct literal and the absence of
    the stale one are pinned, so a partial fix (adding the new phrase without
    removing the old one) still fails."""
    section = _spec_section_8_3()
    assert "work-{fence}" in section
    # The old, run-scoped path must not survive as a standalone claim ANYWHERE
    # in the spec — not just in §8.3, so a stale copy reintroduced elsewhere
    # (§12, a future section) is caught too. Any "runs/<run_id>/work" not
    # immediately followed by "-{fence}" is the stale phrasing this test
    # exists to catch.
    whole_spec = SPEC_PATH.read_text(encoding="utf-8")
    assert re.search(r"runs/<run_id>/work(?!-\{fence\})", whole_spec) is None


# --- c. the format is real, not just documented -------------------------


def test_the_working_root_the_service_builds_matches_work_dash_fence(db, tmp_path, monkeypatch):
    """Read the ACTUAL path a run's leaves get, the way
    ``test_each_acquisition_gets_its_own_working_root`` does (spy on
    ``make_sandboxed_leaf_factory``'s ``working_root`` kwarg — there is no
    other public surface that exposes it) and check it against the literal
    the spec now promises: ``runs/<run_id>/work-<fence-int>``."""
    roots: list = []
    real = service_module.make_sandboxed_leaf_factory

    def spy(**kwargs):
        roots.append(kwargs["working_root"])
        return real(**kwargs)

    monkeypatch.setattr(service_module, "make_sandboxed_leaf_factory", spy)
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()

    assert len(roots) == 1
    root = roots[0]
    assert root.parent.name == run_id
    assert re.fullmatch(r"work-\d+", root.name), root.name
