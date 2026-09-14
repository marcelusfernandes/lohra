"""#99: existing optional toolset/availability contracts, isolated registry only."""

from collections import Counter
from importlib import import_module
from types import SimpleNamespace

import pytest

from lohra.tools.registry import ToolRegistry


@pytest.fixture
def catalog(monkeypatch):
    clock, checks = SimpleNamespace(now=100.0), Counter()
    module = import_module("lohra.tools.registry")
    # Replace only this module's clock reference, not process-wide monotonic.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def check(name, value):
        def available():
            checks[name] += 1
            if isinstance(value, Exception):
                raise value
            return value
        return available

    def never_dispatch(*args, **kwargs):
        raise AssertionError("catalog discovery must not invoke a tool handler")

    registry = ToolRegistry()
    for name, group, available in (
        ("alpha", "file", None),
        ("beta", "web", check("true", True)),
        ("gamma", "web", check("false", False)),
        ("delta", "web", check("raises", RuntimeError("synthetic unavailable"))),
    ):
        registry.register(name, group, {
            "description": "fixture", "parameters": {"type": "object"},
        }, never_dispatch, check_fn=available)
    return registry, clock, checks


def names(definitions):
    return [item["function"]["name"] for item in definitions]


@pytest.mark.parametrize(("enabled", "expected"), [
    (None, ["alpha", "beta"]),
    (set(), []),
    ({"file"}, ["alpha"]),
    ({"web"}, ["beta"]),
    ({"file", "web"}, ["alpha", "beta"]),
    ({"missing"}, []),
    ({"web", "missing"}, ["beta"]),
])
def test_optional_groups_preserve_default_and_availability(catalog, enabled, expected):
    registry, _, checks = catalog
    default = registry.get_definitions()
    selected = None if enabled is None else set(enabled)
    assert names(default) == ["alpha", "beta"]
    assert names(registry.get_definitions(enabled=enabled)) == expected
    assert enabled == selected
    assert registry.get_definitions() == default
    assert checks == {"true": 1, "false": 1, "raises": 1}


def test_excluded_groups_do_not_probe_availability(catalog):
    registry, _, checks = catalog
    assert registry.get_definitions(enabled=set()) == []
    assert registry.get_definitions(enabled={"missing"}) == []
    assert names(registry.get_definitions(enabled={"file"})) == ["alpha"]
    assert checks == {}
    assert names(registry.get_definitions(enabled={"web"})) == ["beta"]
    assert checks == {"true": 1, "false": 1, "raises": 1}


def test_availability_cache_expires_under_a_controlled_clock(catalog):
    registry, clock, checks = catalog
    default = registry.get_definitions()
    clock.now += 29.0
    assert registry.get_definitions() == default
    assert checks == {"true": 1, "false": 1, "raises": 1}
    clock.now += 1.0  # the existing 30-second TTL boundary
    assert registry.get_definitions() == default
    assert checks == {"true": 2, "false": 2, "raises": 2}
