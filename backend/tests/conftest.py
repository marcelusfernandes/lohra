"""Shared test fixtures."""

import os

import pytest


@pytest.fixture(autouse=True)
def _restore_environment():
    """Snapshot and restore os.environ around every test.

    The CLI mutates the process environment by design (``--profile`` sets
    ``LOHRA_PROFILE``; provider resolution reads keys), and those writes bypass
    monkeypatch's record-on-delenv. A full snapshot guarantees no test leaks an
    env var into the next regardless of how it was set.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)
