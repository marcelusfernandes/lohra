"""Tests for resolve_limits — flag > env vars > defaults (configurable, bounded)."""

import pytest

from lohra.orchestration.core import (
    DEFAULT_MAX_CHILDREN,
    DEFAULT_MAX_CONCURRENT,
    resolve_limits,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LOHRA_MAX_PARALLEL", raising=False)
    monkeypatch.delenv("LOHRA_MAX_SUBSESSIONS", raising=False)


def test_defaults_when_nothing_set():
    assert resolve_limits() == (DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_CHILDREN)


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("LOHRA_MAX_PARALLEL", "8")
    monkeypatch.setenv("LOHRA_MAX_SUBSESSIONS", "50")
    assert resolve_limits() == (8, 50)


def test_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("LOHRA_MAX_PARALLEL", "8")
    assert resolve_limits(max_parallel=2)[0] == 2  # flag wins for concurrency


def test_invalid_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LOHRA_MAX_PARALLEL", "not-a-number")
    monkeypatch.setenv("LOHRA_MAX_SUBSESSIONS", "0")  # < 1 is invalid
    assert resolve_limits() == (DEFAULT_MAX_CONCURRENT, DEFAULT_MAX_CHILDREN)


def test_flag_is_clamped_to_at_least_one():
    assert resolve_limits(max_parallel=0)[0] == 1


def test_cli_parses_max_parallel_flag():
    from lohra.cli import build_parser

    args = build_parser().parse_args(["chat", "hi", "--max-parallel", "6"])
    assert args.max_parallel == 6
    # absent -> None, so resolve_limits falls through to env/default
    assert build_parser().parse_args(["chat", "hi"]).max_parallel is None
