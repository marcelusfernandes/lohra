"""The suite never reads the developer's real ``~/.lohra``.

Fatia C gave the operator a price file (``~/.lohra/pricing.json``) that the
production estimator reads by default — so a developer who USES the feature had
three tests turn red on their machine and green on everyone else's. The guard is
a home of the test's own, and these are the tests that keep it there.
"""

from __future__ import annotations

import os
from pathlib import Path

from lohra.memory.paths import lohra_home
from lohra.pricing.overrides import price_overrides_path

_SEEN: set[Path] = set()


def _claim_home() -> Path:
    """Record this test's home and assert no earlier test shared it."""
    home = lohra_home()
    assert home not in _SEEN, "two tests shared one home — the isolation is per SESSION, not per test"
    _SEEN.add(home)
    return home


def test_lohra_home_is_a_private_directory_not_the_developers():
    """Every test runs against a home nobody else wrote to."""
    home = _claim_home()
    assert os.environ.get("LOHRA_HOME"), "LOHRA_HOME must be set for every test"
    assert home != Path.home() / ".lohra"
    assert not price_overrides_path(home).exists()


def test_a_price_file_written_by_one_test_cannot_reach_the_next():
    """Per-test, not per-session: a test that writes a pricing.json (as the
    override tests legitimately do) must not price the next test's models."""
    home = _claim_home()
    written = price_overrides_path(home)
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text('{"openai": {"gpt-4o": {"input_usd": 999, "output_usd": 999}}}')
    assert written.exists()
