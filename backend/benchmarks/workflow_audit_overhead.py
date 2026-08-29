"""Reproduce the OBS-05 end-to-end audit overhead measurement.

Run from ``backend/`` with the project environment active::

    python benchmarks/workflow_audit_overhead.py --samples 9 --warmups 2

The fake provider has zero latency on purpose: this measures a pessimistic upper
bound for the audit share of runtime.  No timing threshold is a test contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import median
import tempfile
import time
from typing import Any

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService


class _InstantClient(ModelClient):
    def create(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
        }

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs: Any) -> dict[str, Any]:
        return self.create(**kwargs)


class _NoAudit:
    def record(self, _event: dict[str, Any]) -> None:
        return None

    def record_gateway(
        self, _frame: dict[str, Any], _context: Any, *, sub_id: str | None = None
    ) -> None:
        return None

    def record_gap(self, _run_id: str, _reason: str, *, count: int | None) -> None:
        return None

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        return True


def _factory() -> Agent:
    return Agent(
        model="benchmark",
        provider=get_provider_profile("anthropic"),
        client=_InstantClient(),
    )


def _sample(*, enabled: bool, leaves: int, pool: int) -> dict[str, float | int]:
    with tempfile.TemporaryDirectory(prefix="lohra-audit-bench-") as raw:
        home = Path(raw)
        path = home / "state.db"
        db = SessionDB(str(path))
        service = WorkflowService(
            base_child_factory=_factory,
            db=db,
            home=home,
            run_concurrency=pool,
            max_runs=1,
        )
        if not enabled:
            assert service._audit.shutdown()
            service._audit = _NoAudit()  # type: ignore[assignment]
        spec = {
            "meta": {"name": "audit-overhead"},
            "nodes": [{
                "id": "wide",
                "type": "parallel",
                "branches": [f"answer item-{index}" for index in range(leaves)],
            }],
        }
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        run_id = service.start(spec)["run_id"]
        result = service.status(run_id, wait=True, timeout=30)
        assert result["status"] == "complete", result
        assert service._audit.flush(timeout=10)
        cpu_ns = time.process_time_ns() - cpu_start
        wall_ns = time.perf_counter_ns() - wall_start
        audit = db.audit_query(run_id, limit=100) if enabled else None
        events = int(audit["page"]["snapshot_seq"]) if audit is not None else 0
        gaps = int(audit["integrity"]["event_markers"]["gaps"]) if audit is not None else 0
        dropped = (
            sum(
                int(notice["data"].get("dropped_count") or 0)
                for notice in audit["integrity"]["notices"]
                if notice["event_type"] == "audit.gap"
            )
            if audit is not None
            else 0
        )
        service.shutdown()
        db.close()
        sqlite_bytes = sum(
            candidate.stat().st_size
            for suffix in ("", "-wal", "-shm")
            if (candidate := Path(f"{path}{suffix}")).exists()
        )
        return {
            "wall_ns": wall_ns,
            "cpu_ns": cpu_ns,
            "events": events,
            "gaps": gaps,
            "dropped": dropped,
            "sqlite_bytes": sqlite_bytes,
        }


def _summarize(rows: list[dict[str, float | int]]) -> dict[str, float | int]:
    return {
        "wall_ms_median": median(float(row["wall_ns"]) for row in rows) / 1_000_000,
        "cpu_ms_median": median(float(row["cpu_ns"]) for row in rows) / 1_000_000,
        "events_median": int(median(int(row["events"]) for row in rows)),
        "samples_with_gaps": sum(int(row["gaps"]) > 0 for row in rows),
        "dropped_total": sum(int(row["dropped"]) for row in rows),
        "sqlite_kib_median": median(float(row["sqlite_bytes"]) for row in rows) / 1024,
    }


def run(*, samples: int, warmups: int, leaves: int, pool: int) -> dict[str, Any]:
    for index in range(warmups):
        for enabled in ((False, True) if index % 2 == 0 else (True, False)):
            _sample(enabled=enabled, leaves=leaves, pool=pool)
    disabled: list[dict[str, float | int]] = []
    enabled: list[dict[str, float | int]] = []
    for index in range(samples):
        pair: dict[bool, dict[str, float | int]] = {}
        for audit_on in ((False, True) if index % 2 == 0 else (True, False)):
            pair[audit_on] = _sample(enabled=audit_on, leaves=leaves, pool=pool)
        disabled.append(pair[False])
        enabled.append(pair[True])
    baseline = _summarize(disabled)
    audited = _summarize(enabled)
    expected_events = 2 * leaves + 6
    wall_delta = median(
        float(on["wall_ns"]) - float(off["wall_ns"])
        for off, on in zip(disabled, enabled, strict=True)
    ) / 1_000_000
    cpu_delta = median(
        float(on["cpu_ns"]) - float(off["cpu_ns"])
        for off, on in zip(disabled, enabled, strict=True)
    ) / 1_000_000
    sqlite_delta = median(
        float(on["sqlite_bytes"]) - float(off["sqlite_bytes"])
        for off, on in zip(disabled, enabled, strict=True)
    ) / 1024
    return {
        "environment": {
            "provider": "fake-zero-latency",
            "leaves": leaves,
            "pool": pool,
            "samples": samples,
            "warmups": warmups,
            "expected_events_per_sample": expected_events,
        },
        "without_audit": baseline,
        "with_audit": audited,
        "delta": {
            "wall_ms": wall_delta,
            "cpu_ms": cpu_delta,
            "sqlite_kib": sqlite_delta,
            "wall_us_per_expected_event": wall_delta * 1000 / expected_events,
            "cpu_us_per_expected_event": cpu_delta * 1000 / expected_events,
        },
        "interpretation": (
            "Pessimistic audit share: the fake provider contributes no network/model latency; "
            "absolute deltas are meaningful, percentage overhead is not representative."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=9)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--leaves", type=int, default=64)
    parser.add_argument("--pool", type=int, default=8)
    args = parser.parse_args()
    if min(args.samples, args.leaves, args.pool) < 1 or args.warmups < 0:
        parser.error("samples, leaves and pool must be positive; warmups cannot be negative")
    print(json.dumps(run(**vars(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
