"""Rendering a workflow run for a human terminal (WF-30).

Pure and append-only, deliberately. No cursor moves, no redraw, no ANSI: the
lines are written to STDERR while the agent's own answer is being streamed to
stdout, and anything that repositioned the cursor would scribble over it. Append
-only also means a scrollback, a pipe and a CI log all read the same.

STDOUT IS NEVER TOUCHED HERE. Under ``lohra chat --json`` stdout carries exactly
one parseable envelope, so every line this module produces goes to stderr in BOTH
modes — the caller passes the stream, and ``write_lines`` is the only writer.

``render_event`` mirrors the sink signature (``run_id, kind, payload``) rather
than reading the run id out of the payload: the id belongs to the delivery, not
to what the engine had to say, and two runs in flight at once make an unlabelled
line unreadable.
"""

from __future__ import annotations

from typing import Any, TextIO

from lohra.workflow.events import DONE, FAULT, ITEMS, NODE, PLAN

# Enough of a run id to pick it out of a listing, short enough to prefix a line.
SHORT_ID = 8

# One glyph per settled state — a line scanned at a glance, not read.
_MARKS = {"running": "▸", "complete": "✓", "null": "✗"}


def short_id(run_id: str) -> str:
    return str(run_id or "?")[:SHORT_ID]


def format_tokens(tokens: Any) -> str:
    """``8.1k tok`` past a thousand, the exact count below it — the number is a
    running cost, and three significant figures is all anyone reads."""
    try:
        count = int(tokens or 0)
    except (TypeError, ValueError):
        return "0 tok"
    return f"{count / 1000:.1f}k tok" if count >= 1000 else f"{count} tok"


def _counts(payload: dict[str, Any]) -> str:
    return f"{int(payload.get('done') or 0)}/{int(payload.get('total') or 0)} nodes"


def _plan_lines(run_id: str, payload: dict[str, Any]) -> list[str]:
    """The DAG as a numbered list, in the order the engine will really take it."""
    name = payload.get("name") or "workflow"
    budget = payload.get("token_budget")
    head = f"workflow {name} ({short_id(run_id)})"
    if budget:
        head += f" · budget {int(budget)} tok"
    lines = [head]
    for position, node in enumerate(payload.get("nodes") or [], start=1):
        # Only the knobs that change what a node COSTS; the rest is spec detail
        # the author already has.
        extras = [
            f"{label} {node[key]}"
            for key, label in (("tier", "tier"), ("model", "model"), ("provider", "via"))
            if node.get(key)
        ]
        shape = ", ".join([str(node.get("type") or "?")] + extras)
        line = f"  {position}. {node.get('id')} ({shape})"
        depends = node.get("depends_on") or []
        if depends:
            line += " <- depends: " + ", ".join(str(dep) for dep in depends)
        lines.append(line)
    for warning in payload.get("warnings") or []:
        lines.append(f"  ⚠ {warning.get('message')}")
    return lines


def render_event(run_id: str, kind: str, payload: dict[str, Any]) -> list[str]:
    """The lines one live event is worth — empty for a kind this build does not
    render, so a newer emitter can never crash an older terminal."""
    prefix = f"[{short_id(run_id)}]"
    if kind == PLAN:
        return _plan_lines(run_id, payload)
    if kind == NODE:
        mark = _MARKS.get(str(payload.get("state")), "·")
        return [
            f"{prefix} {payload.get('node_id')} {mark} · {_counts(payload)} · "
            f"{format_tokens(payload.get('tokens'))}"
        ]
    if kind == ITEMS:
        done = int(payload.get("done") or 0)
        total = int(payload.get("total") or 0)
        line = f"{prefix} {payload.get('node_id')} · items {done}/{total}"
        if payload.get("tokens") is not None:  # custo já pousado escala item a item
            line += " · " + format_tokens(int(payload["tokens"]))
        return [line]
    if kind == FAULT:
        return [f"{prefix} ⚠ {payload.get('text')}"]
    if kind == DONE:
        name = payload.get("name") or "workflow"
        return [
            f"{prefix} workflow {name} finished: {payload.get('status')} · "
            f"{_counts(payload)} · {format_tokens(payload.get('tokens'))}"
        ]
    return []


def render_run_row(entry: dict[str, Any]) -> str:
    """One line of ``lohra workflow list`` / one tick of ``watch``."""
    marks = " (stale)" if entry.get("stale") else ""
    budget = entry.get("token_budget")
    spend = f"{int(entry.get('tokens_spent') or 0)}"
    if budget:
        spend += f"/{int(budget)}"
    return (
        f"{short_id(entry.get('run_id') or '')}  {entry.get('status')}{marks}  "
        f"{int(entry.get('nodes_done') or 0)}/{int(entry.get('nodes_total') or 0)} nodes  "
        f"{spend} tok  {entry.get('name') or ''}".rstrip()
    )


def _ascii(line: str) -> str:
    """The same line, with the glyphs a byte-limited terminal cannot take."""
    folded = line
    # EVERY fold is ONE character, ``✓`` included (it used to be ``ok``). The
    # block mode truncates a line to exactly the terminal width, so a fold that
    # GREW a line by one would wrap it — and a wrapped line is precisely what the
    # block's cursor arithmetic cannot survive. ``─`` and ``…`` are its glyphs too.
    for glyph, plain in (
        ("▸", ">"), ("✓", "+"), ("✗", "x"), ("⚠", "!"), ("·", "-"), ("─", "-"), ("…", "."),
    ):
        folded = folded.replace(glyph, plain)
    return folded.encode("ascii", "replace").decode("ascii")


def write_lines(lines: list[str], stream: TextIO) -> None:
    """Write the live view out. A progress line must never be the thing that
    kills a turn, so a stream that cannot encode the glyphs (a C-locale
    terminal, a redirected pipe with an ascii encoding) gets the folded text
    instead of a UnicodeEncodeError, and a stream that is closed or gone is
    simply not written to."""
    for line in lines:
        try:
            stream.write(line + "\n")
        except UnicodeEncodeError:
            stream.write(_ascii(line) + "\n")
        except (ValueError, OSError):
            return  # closed/detached stream: the run is not the terminal's problem
    try:
        stream.flush()
    except (ValueError, OSError):
        pass
