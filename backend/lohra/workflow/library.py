"""Self-improvement: run outcomes feed back into memory + a template library (§12).

Lohra is self-improving — workflow authoring shouldn't be a static tool-description
problem; run OUTCOMES must inform the next authoring. Two durable mechanisms,
written by TRUSTED engine code (never a leaf, §8.3):

- §12.2 a problematic run distills into a workflow-INSIGHTS prior ("what specs
  fail"). These are machine-generated telemetry, so they live in their OWN file
  (~/.lohra/workflows/insights.md) — NOT the user-curated MEMORY.md and NOT the
  frozen prompt. The agent reads them on demand via the workflow_templates tool.
- §12.3 a clean, validated, low-null-rate run is saved as a reusable TEMPLATE;
  the agent is steered to retrieve + adapt one before authoring from scratch.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

from lohra.workflow.accounting import RunResult

logger = logging.getLogger(__name__)

# Only specs that completed this clean become reusable templates.
TEMPLATE_NULL_RATE_MAX = 0.2
# Keep the insights log bounded (machine telemetry, newest kept).
MAX_INSIGHTS = 200
# How much of the run's own faults a prior quotes (WF-25). Two causes are a
# diagnosis; ten are a log. Each is clipped so one traceback cannot eat the file.
MAX_QUOTED_FAULTS = 2
MAX_FAULT_CHARS = 120
_INSIGHTS_LOCK = threading.Lock()  # serialize concurrent run completions in-process


def _templates_dir(home: Path) -> Path:
    return home / "workflows" / "templates"


def _insights_path(home: Path) -> Path:
    return home / "workflows" / "insights.md"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or "workflow"


def _shape(spec: dict) -> str:
    nodes = spec.get("nodes") if isinstance(spec, dict) else None
    types = sorted({n.get("type") for n in nodes if isinstance(n, dict)}) if isinstance(nodes, list) else []
    return "+".join(t for t in types if t) or "?"


def record_outcome(
    home: Path,
    spec: dict,
    result: RunResult,
    *,
    tokens_total: int | None = None,
    faults_total: list[str] | None = None,
    prior_degraded: bool = False,
) -> None:
    """On run completion: a problematic run → a MemoryStore prior; a clean run →
    a reusable template. Never raises into the caller (best-effort feedback).

    ``tokens_total`` is the WHOLE run's cumulative cost (WF-23); ``result`` only
    carries the stretch since the last launch, so a resumed run would otherwise
    teach the library it had been cheap. Omitted → the result's own figures.

    ``faults_total`` is the same correction for the causes a prior quotes
    (WF-25/26), and ``prior_degraded`` for the VERDICT: a run that really failed
    in an earlier stretch and then finished its last one cleanly is not a clean
    run, and certifying it as a reusable template would publish a spec whose own
    telemetry says it broke. A PAUSE is not that kind of failure — the caller
    discounts it — so the ordinary resumed run stays eligible."""
    name = (spec.get("meta") or {}).get("name", "workflow") if isinstance(spec, dict) else "workflow"
    try:
        if (
            prior_degraded
            or result.status != "complete"
            or result.null_rate > TEMPLATE_NULL_RATE_MAX
        ):
            _record_prior(home, name, spec, result, tokens_total, faults_total)
        else:
            _save_template(home, name, spec)
    except Exception:  # feedback must never break a finished run
        logger.exception("workflow: record_outcome failed for %s", name)


def _clip(fault: str) -> str:
    """One fault as ONE bounded line. insights.md is line-oriented — a fault
    carrying a traceback would split into rows that break both the dedup and the
    cap that keep this file readable."""
    flat = " ".join(str(fault).split())
    return flat if len(flat) <= MAX_FAULT_CHARS else flat[: MAX_FAULT_CHARS - 1] + "…"


def _causes(faults: list[str]) -> str:
    """What actually went wrong, quoted (WF-25).

    The advice that follows is generic by nature; the faults are not. A prior
    that only ever said "add a verify stage" taught the next authoring nothing
    about THIS run — the dogfood's cascade of "upstream null: args.source" was
    invisible in its own telemetry."""
    quoted = "; ".join(_clip(fault) for fault in faults[:MAX_QUOTED_FAULTS] if str(fault).strip())
    return f" Faults: {quoted}." if quoted else ""


def _record_prior(
    home: Path,
    name: str,
    spec: dict,
    result: RunResult,
    tokens_total: int | None = None,
    faults_total: list[str] | None = None,
) -> None:
    issues = []
    if result.null_rate:
        issues.append(f"null_rate {result.null_rate:.0%}")
    if result.cap_trips:
        issues.append(f"{result.cap_trips} cap-trip(s)")
    if result.engine_faults:
        issues.append(f"{result.engine_faults} engine-fault(s)")
    if result.validation_retries:
        issues.append(f"{result.validation_retries} validation-retr(ies)")
    if result.forcing_fallbacks:
        issues.append(f"{result.forcing_fallbacks} forced-output fallback(s)")
    tokens = tokens_total if tokens_total is not None else result.tokens_in + result.tokens_out
    if tokens:
        issues.append(f"~{tokens} tokens")
    entry = (
        f"- [{name}] shape {_shape(spec)} → {result.status}"
        + (f"; {', '.join(issues)}" if issues else "")
        + "."
        + _causes(result.faults if faults_total is None else faults_total)
        + " Revise: add a verify stage / schemas / tighter fan-out."
    )
    path = _insights_path(home)
    with _INSIGHTS_LOCK:  # serialize the read-modify-write across concurrent runs
        existing = _read_lines(path)
        if entry in existing:  # dedup: don't pile up identical priors
            return
        existing.append(entry)
        kept = existing[-MAX_INSIGHTS:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def _read_lines(path: Path) -> list[str]:
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def recent_insights(home: Path, limit: int = 20) -> list[str]:
    """The most recent workflow priors (what shapes failed) — read on demand."""
    return _read_lines(_insights_path(home))[-limit:]


def _save_template(home: Path, name: str, spec: dict) -> None:
    directory = _templates_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{_safe_name(name)}.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8"
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
        out.append({"name": path.stem, "description": meta.get("description", "")})
    return out


def get_template(home: Path, name: str) -> dict | None:
    """The full validated spec for a template, ready to adapt and re-run."""
    path = _templates_dir(home) / f"{_safe_name(name)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
