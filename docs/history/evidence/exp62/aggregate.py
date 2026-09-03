"""Agrega os JSONL do experimento #62 numa tabela por configuração."""

from __future__ import annotations

import collections
import json
import pathlib
import sys

RESULTS = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "results")


def summarize(path: pathlib.Path) -> dict:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    out = {
        "n": len(rows),
        "outcomes": dict(collections.Counter(r.get("outcome") for r in rows)),
        "status": dict(collections.Counter(r.get("status") for r in rows)),
        "overlapped": sum(1 for r in rows if r.get("overlapped")),
        "faults_nonzero": sum(1 for r in rows if r.get("faults")),
        "fault_samples": sorted({f[:120] for r in rows for f in (r.get("faults") or [])})[:3],
        "reader_saw": dict(collections.Counter(
            _reader_class(r) for r in rows
        )),
        "artifact_verdicts": dict(collections.Counter(
            c.get("artifact_verification") for r in rows for c in (r.get("cells") or [])
        )),
        "resume": {
            "spawns": [r["resume_spawns"] for r in rows if "resume_spawns" in r],
            "status": [r.get("resume_status") for r in rows if "resume_status" in r],
            "faults": [r.get("resume_faults") for r in rows if "resume_faults" in r],
        },
        "cells_per_run": dict(collections.Counter(len(r.get("cells") or []) for r in rows)),
    }
    return out


def _reader_class(row: dict) -> str:
    saw = row.get("reader_saw")
    if isinstance(saw, dict):
        if "saw" in saw:
            text = saw.get("saw")
            if text is None:
                return f"error:{(saw.get('error') or '')[:40]}"
            lines = [x for x in text.splitlines() if x.strip()]
            if not lines:
                return "empty(intermediate)"
            return f"{len(lines)}line(s):{'+'.join(x[-1] for x in lines)}"
        if "a" in saw:  # config D
            a_ok = saw.get("a") is not None
            b_ok = saw.get("b") is not None
            return f"a={'ok' if a_ok else 'MISSING'},b={'ok' if b_ok else 'MISSING'}"
    return "n/a"


def main() -> None:
    report = {}
    for path in sorted(RESULTS.glob("*.jsonl")):
        report[path.stem] = summarize(path)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
