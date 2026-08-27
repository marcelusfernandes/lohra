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


@pytest.fixture(autouse=True)
def _no_real_ollama_probe():
    """Hermetic by default: no test ever asks a real local daemon anything.

    ``detect.default_probe`` is the single seam every ambient consumer (provider
    resolution, the wizard gate, ``lohra doctor``) goes through, so neutralizing
    that one name keeps the suite deterministic on a developer machine that
    happens to be running Ollama. A test that WANTS a live daemon injects one —
    either by parameter (``probe=``/``ollama_probe=``) or by re-patching this
    same name, which cleanly overrides this fixture.

    ``probe_ollama`` itself is untouched: its own tests exercise the real
    implementation against an httpx MockTransport.

    Patched BY HAND, deliberately: an autouse fixture that requests ``monkeypatch``
    makes monkeypatch a dependency of the autouse chain, which inverts teardown
    order and lets a monkeypatch restore run AFTER the environment snapshot above
    — resurrecting a variable the CLI wrote (``LOHRA_PROFILE``) into the next
    test. That is exactly the leak the snapshot fixture exists to prevent.
    """
    from lohra.onboarding import detect

    original = detect.default_probe
    detect.default_probe = lambda: detect.OllamaStatus(
        alive=False, url=detect.OLLAMA_TAGS_URL, detail="probe disabled in tests"
    )
    try:
        yield
    finally:
        detect.default_probe = original
