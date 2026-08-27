"""Tests for the 3-tier system prompt builder (Invariante #1).

Spec §7: built once per session, frozen; ordered stable -> context -> volatile
to keep the provider prefix cache warm. Volatile timestamp is date-only.
"""

import dataclasses
import datetime

import pytest

from lohra.agent.system_prompt import (
    DEFAULT_IDENTITY,
    SystemPromptSnapshot,
    build_system_prompt,
)

TODAY = datetime.date(2026, 6, 9)


def test_tiers_appear_in_stable_context_volatile_order():
    snapshot = build_system_prompt(
        system_message="caller instructions",
        today=TODAY,
    )
    text = snapshot.text
    assert text.index(snapshot.stable) < text.index(snapshot.context)
    assert text.index(snapshot.context) < text.index(snapshot.volatile)


def test_stable_tier_contains_identity():
    snapshot = build_system_prompt(today=TODAY)
    assert DEFAULT_IDENTITY in snapshot.stable
    assert "Lohra" in snapshot.stable


def test_byte_stable_for_same_inputs():
    a = build_system_prompt(system_message="x", today=TODAY)
    b = build_system_prompt(system_message="x", today=TODAY)
    assert a.text == b.text


def test_volatile_timestamp_is_date_only():
    snapshot = build_system_prompt(today=TODAY)
    assert "2026-06-09" in snapshot.volatile
    # No clock time — minutes would invalidate the prefix cache every request.
    assert ":" not in snapshot.volatile.split("2026-06-09")[1].splitlines()[0]


def test_snapshot_is_frozen():
    snapshot = build_system_prompt(today=TODAY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.stable = "mutated"  # type: ignore[misc]


def test_caller_system_message_lands_in_context_tier():
    snapshot = build_system_prompt(system_message="be terse", today=TODAY)
    assert "be terse" in snapshot.context
    assert "be terse" not in snapshot.stable


def test_context_files_are_labeled_and_included():
    snapshot = build_system_prompt(
        context_files=(("AGENTS.md", "follow the roadmap"),),
        today=TODAY,
    )
    assert "AGENTS.md" in snapshot.context
    assert "follow the roadmap" in snapshot.context


def test_memory_and_user_profile_land_in_volatile_tier():
    snapshot = build_system_prompt(
        memory_snapshot="user prefers tabs",
        user_profile="name: Marcelus",
        today=TODAY,
    )
    assert "user prefers tabs" in snapshot.volatile
    assert "name: Marcelus" in snapshot.volatile
    assert "user prefers tabs" not in snapshot.stable


def test_empty_tier_content_is_omitted_cleanly():
    snapshot = build_system_prompt(today=TODAY)
    assert snapshot.context == ""
    assert "\n\n\n" not in snapshot.text


def test_environment_hints_in_stable_tier():
    snapshot = build_system_prompt(
        environment_hints={"platform": "darwin", "cwd": "/tmp/x"},
        today=TODAY,
    )
    assert "darwin" in snapshot.stable
    assert "/tmp/x" in snapshot.stable


def test_environment_hints_serialized_deterministically():
    a = build_system_prompt(environment_hints={"b": "2", "a": "1"}, today=TODAY)
    b = build_system_prompt(environment_hints={"a": "1", "b": "2"}, today=TODAY)
    assert a.text == b.text


def test_today_defaults_to_current_date():
    snapshot = build_system_prompt()
    assert datetime.date.today().isoformat() in snapshot.volatile


def test_snapshot_text_is_cached_and_consistent():
    snapshot = build_system_prompt(system_message="x", today=TODAY)
    assert isinstance(snapshot, SystemPromptSnapshot)
    assert snapshot.text == snapshot.text
    assert snapshot.stable in snapshot.text
