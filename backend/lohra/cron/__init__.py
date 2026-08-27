"""Cron / scheduled jobs (spec §6).

Jobs live in ``HOME/cron/jobs.json``; the scheduler ticks ~every 60s and runs
due jobs as forked agents. Schedule computation is pure and clock-injected so it
is fully testable without threads or sleeping.
"""
