"""Tests for cron schedule computation (pure, no clock/threads).

Job dict shape: {id, name, prompt, type: once|interval|cron, value, enabled,
created_at, last_run_at}. ``value`` is run_at (once), minutes (interval), or a
5-field cron expression (cron).
"""

from datetime import datetime

import pytest

from lohra.cron.schedule import cron_matches, is_due, parse_cron_field


# --- parse_cron_field ---


def test_field_star_is_full_range():
    assert parse_cron_field("*", 0, 5) == {0, 1, 2, 3, 4, 5}


def test_field_single_value():
    assert parse_cron_field("3", 0, 59) == {3}


def test_field_list():
    assert parse_cron_field("1,5,9", 0, 59) == {1, 5, 9}


def test_field_range():
    assert parse_cron_field("2-5", 0, 59) == {2, 3, 4, 5}


def test_field_step_over_star():
    assert parse_cron_field("*/15", 0, 59) == {0, 15, 30, 45}


def test_field_step_over_range():
    assert parse_cron_field("0-20/10", 0, 59) == {0, 10, 20}


def test_field_invalid_raises():
    with pytest.raises(ValueError):
        parse_cron_field("abc", 0, 59)


def test_field_out_of_range_raises():
    with pytest.raises(ValueError, match="out of range"):
        parse_cron_field("70", 0, 59)


# --- cron_matches (5 fields: min hour dom month dow; dow 0=Sunday) ---


def test_every_minute_always_matches():
    assert cron_matches("* * * * *", datetime(2026, 6, 15, 13, 37)) is True


def test_specific_minute_and_hour():
    expr = "30 9 * * *"  # 09:30 daily
    assert cron_matches(expr, datetime(2026, 6, 15, 9, 30)) is True
    assert cron_matches(expr, datetime(2026, 6, 15, 9, 31)) is False
    assert cron_matches(expr, datetime(2026, 6, 15, 10, 30)) is False


def test_day_of_week_sunday_is_zero():
    expr = "0 0 * * 0"  # midnight on Sunday
    # 2026-06-14 is a Sunday
    assert cron_matches(expr, datetime(2026, 6, 14, 0, 0)) is True
    # 2026-06-15 is a Monday
    assert cron_matches(expr, datetime(2026, 6, 15, 0, 0)) is False


def test_weekday_7_is_also_sunday():
    # standard cron: 7 == 0 == Sunday. 2026-06-14 is a Sunday.
    assert cron_matches("0 0 * * 7", datetime(2026, 6, 14, 0, 0)) is True
    assert cron_matches("0 0 * * 7", datetime(2026, 6, 15, 0, 0)) is False  # Monday


def test_step_minutes():
    expr = "*/15 * * * *"
    assert cron_matches(expr, datetime(2026, 6, 15, 8, 45)) is True
    assert cron_matches(expr, datetime(2026, 6, 15, 8, 46)) is False


def test_malformed_expression_raises():
    with pytest.raises(ValueError):
        cron_matches("* * *", datetime(2026, 6, 15, 0, 0))


# --- is_due ---


def _job(**kw):
    base = {
        "id": "j1",
        "name": "test",
        "prompt": "do it",
        "type": "interval",
        "value": 5,
        "enabled": True,
        "created_at": 0.0,
        "last_run_at": None,
    }
    base.update(kw)
    return base


def test_disabled_job_is_never_due():
    assert is_due(_job(enabled=False), now=10_000.0) is False


def test_once_due_at_or_after_run_at_only_once():
    run_at = 1000.0
    assert is_due(_job(type="once", value=run_at, last_run_at=None), now=999.0) is False
    assert is_due(_job(type="once", value=run_at, last_run_at=None), now=1000.0) is True
    # already ran -> never again
    assert is_due(_job(type="once", value=run_at, last_run_at=1000.0), now=5000.0) is False


def test_interval_due_when_never_run():
    assert is_due(_job(type="interval", value=5, last_run_at=None), now=0.0) is True


def test_interval_due_after_period_elapsed():
    job = _job(type="interval", value=5, last_run_at=1000.0)  # every 5 min
    assert is_due(job, now=1000.0 + 4 * 60) is False  # only 4 min later
    assert is_due(job, now=1000.0 + 5 * 60) is True  # 5 min later


def test_cron_due_when_minute_matches_and_not_run_this_minute():
    # 13:37 on 2026-06-15
    now = datetime(2026, 6, 15, 13, 37, 5).timestamp()
    job = _job(type="cron", value="37 13 * * *", last_run_at=None)
    assert is_due(job, now=now) is True


def test_cron_not_due_when_minute_does_not_match():
    now = datetime(2026, 6, 15, 13, 38, 0).timestamp()
    job = _job(type="cron", value="37 13 * * *", last_run_at=None)  # only fires at 13:37
    assert is_due(job, now=now) is False


def test_unknown_job_type_raises():
    with pytest.raises(ValueError, match="unknown job type"):
        is_due(_job(type="weekly", value=1), now=1000.0)


def test_cron_not_due_twice_in_same_minute():
    now = datetime(2026, 6, 15, 13, 37, 50).timestamp()
    ran_this_minute = datetime(2026, 6, 15, 13, 37, 1).timestamp()
    job = _job(type="cron", value="37 13 * * *", last_run_at=ran_this_minute)
    assert is_due(job, now=now) is False
