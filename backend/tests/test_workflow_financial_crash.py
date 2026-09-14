"""A process killed at the commit boundary retains exactly its durable floor."""

import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread

import pytest

from lohra.state import SessionDB
from tests.test_workflow_post_drain_accounting import VECTOR, meter


@pytest.mark.parametrize("phase", ["before", "after"])
def test_process_death_preserves_only_committed_financial_usage(tmp_path, phase):
    backend = str(Path(__file__).resolve().parents[1])
    # Preserve the two user roots verbatim, but pass no provider credentials,
    # session keys or personal tool configuration to the owned subprocess.
    env = {name: os.environ[name] for name in ("HOME", "CODEX_HOME", "TMPDIR") if name in os.environ}
    env.update(PATH=str(Path(sys.executable).parent), PYTHONPATH=backend,
               PYTHONDONTWRITEBYTECODE="1", LOHRA_HOME=str(tmp_path / "lohra-home"))
    received, lines = Event(), []
    with (tmp_path / "child.stderr").open("w+") as stderr:
        child = subprocess.Popen(
            [sys.executable, "-m", "tests.financial_crash_child", str(tmp_path), phase],
            cwd=backend, env=env, stdout=subprocess.PIPE, stderr=stderr, text=True,
        )

        def read_ready():
            lines.append(child.stdout.readline())
            received.set()

        reader = Thread(target=read_ready, daemon=True)
        reader.start()
        try:
            assert received.wait(10), "child did not reach its commit boundary"
            if not lines[0]:
                stderr.seek(0)
                pytest.fail(stderr.read())
            observed = json.loads(lines[0])
            assert observed["phase"] == phase
            assert observed["sealed_input"] == 11 and observed["budget"] == 48
            assert child.poll() is None
            child.kill()
            assert child.wait(timeout=5) < 0
            factor = 1 if phase == "before" else 2
            expected = tuple(factor * value for value in VECTOR)
            assert tuple(observed["row"]) == expected
            db = SessionDB(tmp_path / "state.db")
            try:
                assert meter(db, observed["run_id"]) == expected
                # The functional decision was already persisted in BOTH cases.
                assert db.run_state_get(observed["run_id"])["status"] == "degraded"
            finally:
                db.close()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdout.close()
            reader.join(timeout=5)
