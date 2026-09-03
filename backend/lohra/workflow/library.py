"""Self-improvement: clean run outcomes become a template library (§12).

Legacy automatic learning is DISABLED: problematic runs (quota/timeout/process
loss, degraded finishes, high null rates) no longer distill into a
workflow-INSIGHTS prior and no longer write ~/.lohra/workflows/insights.md.
That file is legacy: it is never written or exposed here. Existing bytes stay
untouched for rollback/audit, while the active surface reads only causally gated
SQLite candidates through ``WorkflowService.recent_insights``.

The surviving mechanism (§12.3): a clean, validated, low-null-rate run is
saved as a reusable TEMPLATE; the agent is steered to retrieve + adapt one
before authoring from scratch.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from lohra.workflow.accounting import RunResult

logger = logging.getLogger(__name__)

# Only specs that completed this clean become reusable templates.
TEMPLATE_NULL_RATE_MAX = 0.2


def _templates_dir(home: Path) -> Path:
    return home / "workflows" / "templates"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "workflow"


def record_outcome(
    home: Path,
    spec: dict,
    result: RunResult,
    *,
    tokens_total: int | None = None,
    faults_total: list[str] | None = None,
    prior_degraded: bool = False,
    leaf_respawns: int = 0,
) -> None:
    """On run completion: a problematic run → a MemoryStore prior; a clean run →
    a reusable template. Never raises into the caller (best-effort feedback).

    ``tokens_total`` is the WHOLE run's cumulative cost (WF-23); ``result`` only
    carries the stretch since the last launch, so a resumed run would otherwise
    teach the library it had been cheap. Omitted → the result's own figures.

    ``prior_degraded`` feeds the VERDICT: a run that really
    failed in an earlier stretch and then finished its last one cleanly is not a
    clean run, and certifying it as a reusable template would publish a spec
    whose own telemetry says it broke. A PAUSE is not that kind of failure — the
    caller discounts it — so the ordinary resumed run stays eligible.

    ``leaf_respawns`` is what surviving the provider COST (Q2, #43). A run whose
    leaf died on attempt 1 and answered on attempt 2 now reaches here as
    ``complete``, and it SHOULD: what failed was the provider, not the shape —
    the spec asked for a re-spawn, got one, and produced its outputs. Certifying
    it silently would be the other half of the old bug, though, so the count
    rides into the template's own ``meta``: the next author retrieving it reads
    "this works, and it cost N extra leaves" instead of inferring a free run.
    ``meta`` is the right home because the validator accepts extra literal keys
    there and the cell namespace reads only ``name``/``version`` — the template
    stays runnable byte-for-byte.

    A PROBLEMATIC verdict writes NOTHING anywhere (legacy insights learning is
    disabled); only the template path can touch disk."""
    name = (
        (spec.get("meta") or {}).get("name", "workflow") if isinstance(spec, dict) else "workflow"
    )
    try:
        if (
            prior_degraded
            or result.status != "complete"
            or result.null_rate > TEMPLATE_NULL_RATE_MAX
        ):
            logger.info("workflow: %s was problematic; nothing learned (legacy insights off)", name)
        else:
            _save_template(home, name, spec, leaf_respawns=leaf_respawns)
    except Exception:  # feedback must never break a finished run
        logger.exception("workflow: record_outcome failed for %s", name)


def recent_insights(home: Path, limit: int = 20) -> list[str]:
    """Legacy compatibility hook; ungated ``insights.md`` is intentionally hidden."""
    del home, limit
    return []


def _save_template(home: Path, name: str, spec: dict, *, leaf_respawns: int = 0) -> None:
    """Write the spec as a template, stamped with what the certifying run cost.

    A NEW dict, never the caller's: ``spec`` is the live run's own spec and the
    service still holds it."""
    directory = _templates_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    meta = spec.get("meta") if isinstance(spec.get("meta"), dict) else {}
    stamped = {**spec, "meta": {**meta, "leaf_respawns": int(leaf_respawns)}}
    (directory / f"{_safe_name(name)}.json").write_text(
        json.dumps(stamped, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def list_templates(home: Path) -> list[dict[str, Any]]:
    """Validated templates available to retrieve + adapt: [{name, description}]."""
    directory = _templates_dir(home)
    if not directory.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta = spec.get("meta") or {}
        out.append(
            {
                "name": path.stem,
                "description": meta.get("description", ""),
                # What the certifying run paid in extra leaves (Q2, #43) — the
                # difference between a template that ran clean and one that only
                # got there because the harness re-spawned for it.
                "leaf_respawns": int(meta.get("leaf_respawns") or 0),
            }
        )
    return out


def get_template(home: Path, name: str) -> dict | None:
    """The full validated spec for a template, ready to adapt and re-run."""
    path = _templates_dir(home) / f"{_safe_name(name)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
