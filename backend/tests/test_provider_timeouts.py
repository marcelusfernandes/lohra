"""Tests for the operator-configurable provider HTTP read timeout (issue #48).

``resolve_provider_timeout`` is the ONLY place that reads
``LOHRA_PROVIDER_READ_TIMEOUT`` — pure, no network, no real env mutation
(everything goes through an injected mapping).
"""

import httpx
import pytest

from lohra.providers.timeouts import (
    DEFAULT_READ_TIMEOUT_SECONDS,
    ENV_VAR,
    effective_read_timeout_seconds,
    resolve_provider_timeout,
)


def test_unset_env_resolves_to_none():
    assert resolve_provider_timeout({}) is None


def test_valid_int_string_builds_a_timeout():
    timeout = resolve_provider_timeout({ENV_VAR: "45"})
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 45.0
    assert timeout.write == 45.0
    assert timeout.pool == 45.0
    assert timeout.connect == 5.0


def test_valid_float_string_builds_a_timeout():
    timeout = resolve_provider_timeout({ENV_VAR: "12.5"})
    assert timeout.read == 12.5


@pytest.mark.parametrize("raw", ["not-a-number", "", "  ", "NaN-ish"])
def test_non_numeric_value_ignored_with_warning(raw, caplog):
    with caplog.at_level("WARNING"):
        assert resolve_provider_timeout({ENV_VAR: raw}) is None
    assert ENV_VAR in caplog.text


def test_zero_is_rejected_with_warning(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_provider_timeout({ENV_VAR: "0"}) is None
    assert ENV_VAR in caplog.text


def test_negative_is_rejected_with_warning(caplog):
    with caplog.at_level("WARNING"):
        assert resolve_provider_timeout({ENV_VAR: "-5"}) is None
    assert ENV_VAR in caplog.text


def test_effective_read_timeout_falls_back_to_default_when_unset():
    assert effective_read_timeout_seconds({}) == DEFAULT_READ_TIMEOUT_SECONDS


def test_effective_read_timeout_reflects_a_valid_override():
    assert effective_read_timeout_seconds({ENV_VAR: "90"}) == 90.0


def test_effective_read_timeout_falls_back_when_invalid():
    assert effective_read_timeout_seconds({ENV_VAR: "nope"}) == DEFAULT_READ_TIMEOUT_SECONDS


def test_reads_real_os_environ_when_no_mapping_given(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "33")
    timeout = resolve_provider_timeout()
    assert timeout.read == 33.0
    monkeypatch.delenv(ENV_VAR)
    assert resolve_provider_timeout() is None
