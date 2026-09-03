"""The same workflow run, redrawn IN PLACE (WF-31) — the block mode.

``liveview`` prints one line per event and never looks back: a scrollback, a
pipe and a CI log all read the same, which is exactly why it stays the DEFAULT
off a terminal and the fallback everywhere else. On a real terminal it buries
the screen — a wide fan-out is a hundred lines saying almost the same thing.

So: a status BLOCK that rewrites itself where it stands (docker, npm, Claude
Code), and freezes when the run ends so the agent's own answer lands underneath
it instead of on top of it.

The seam is strict and load-bearing:

- ``LiveBlock`` keeps the run's state and turns it into a frame — a PURE
  ``compute_frame(width, max_lines)``, so every layout decision is testable with
  no terminal anywhere near it;
- ``LivePainter`` is thin and dumb: cursor-up N, clear to end of screen, rewrite.
  It owns no layout, only arithmetic.

Two facts the arithmetic rests on, and both are inputs to the pure function
rather than assumptions: a line WIDER than the terminal wraps, and a block
TALLER than it clamps at the top — either one and the next repaint starts
scribbling into the scrollback.

What is permanent and what is not: ``fault`` text goes to the SCROLLBACK (the
block only keeps the count), and the last frame of a run is left on screen for
good. Everything else is transient by design.

STDOUT IS NEVER TOUCHED HERE — same contract as ``liveview``. And nothing here
raises: a progress line is never the thing that kills a turn.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, TextIO

from lohra.workflow.events import DONE, FAULT, ITEMS, NODE, PLAN
from lohra.workflow.liveview import (
    _MARKS,
    REPLAY_MARK,
    format_tokens,
    render_event,
    write_lines,
)

logger = logging.getLogger(__name__)

CSI = "\x1b["

# ~10 fps. Fast enough to read as live, slow enough that a fan-out settling a
# dozen items in a millisecond costs one repaint instead of a dozen.
REDRAW_INTERVAL = 0.1

FANCY = "fancy"  # the block, redrawn in place
PLAIN = "plain"  # the append lines of ``liveview`` — the fallback
OFF = "off"  # silence
_MODES = (FANCY, PLAIN, OFF)

# What a node line says when there is no fan-out count to say instead.
_STATE_WORDS = {
    "pending": "waiting",
    "running": "running",
    "complete": "complete",
    "null": "null",
    # It never ran: a `required: true` node upstream of it failed (issue #15).
    "skipped": "skipped",
}
_PENDING = "pending"
# A run can stop (cancelled, faulted) with nodes that never settled. The last
# frame is permanent scrollback, so it says they STOPPED rather than freezing a
# line that claims a node is still running long after the run ended.
_UNSETTLED = ("pending", "running")
_STOPPED = "stopped"

# The frame's last line, and the only place the RUN's spend is allowed to appear.
_RULE = "─"
_ELLIPSIS = "…"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# Every control character becomes ONE space: same length, so nothing below has
# to think about it, and a frame line stays a frame line. C0 + DEL + C1, and
# none of them collides with a glyph the block draws (·, ─, …, the marks).
_CONTROLS = dict.fromkeys([*range(0x20), 0x7F, *range(0x80, 0xA0)], " ")


def sanitize(line: str) -> str:
    """The line with nothing in it that can render as more than it counts.

    Load-bearing, not cosmetic: a frame line is exactly ONE terminal row and the
    painter moves the cursor up by the number of them. ``schema`` only asks a
    node id to be a non-empty string — and the agent authors the spec — so an id
    may carry a newline (two rows, one counted), a tab (wider than it counts) or
    an ESC (moving the very cursor the block owns). One space each.
    """
    return str(line).translate(_CONTROLS)


def truncate(line: str, width: int) -> str:
    """A line that wraps breaks the cursor arithmetic, so it never wraps."""
    clean = sanitize(line)
    limit = max(1, _int(width, 80))
    if len(clean) <= limit:
        return clean
    if limit == 1:
        return _ELLIPSIS
    return clean[: limit - 1] + _ELLIPSIS


class LiveBlock:
    """One run's live state, and the frames it is worth.

    Mutable on purpose — it is an accumulator fed by a stream of events — but it
    replaces its entries instead of editing them in place, and ``compute_frame``
    reads and never writes.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = str(run_id or "")
        self.name = ""
        self.finished = False
        self.status = ""
        self._order: tuple[str, ...] = ()
        self._nodes: dict[str, dict[str, Any]] = {}
        self._done = 0
        self._total = 0
        self._tokens = 0
        self._faults = 0

    # --- fed by the event stream -----------------------------------------

    def update(self, kind: str, payload: dict[str, Any]) -> None:
        """Absorb one live event. Total: a kind this build does not know is a
        kind it ignores, never a crash mid-run."""
        if kind == PLAN:
            self._absorb_plan(payload)
        elif kind == NODE:
            self._absorb_node(payload)
        elif kind == ITEMS:
            self._absorb_items(payload)
        elif kind == FAULT:
            self._faults += 1
        elif kind == DONE:
            self._absorb_done(payload)

    def _absorb_plan(self, payload: dict[str, Any]) -> None:
        nodes = payload.get("nodes") or []
        self.name = str(payload.get("name") or "workflow")
        self._order = tuple(str(node.get("id") or "?") for node in nodes)
        self._nodes = {
            str(node.get("id") or "?"): self._fresh(str(node.get("type") or "?"))
            for node in nodes
        }
        self._total = len(self._order)

    def _absorb_node(self, payload: dict[str, Any]) -> None:
        node_id = str(payload.get("node_id") or "?")
        entry = self._entry(node_id)
        self._nodes = {
            **self._nodes,
            node_id: {
                **entry,
                "state": str(payload.get("state") or _PENDING),
                # Sticky: a node emits RUNNING before it emits COMPLETE, and only
                # the second one can know its cell came from the cache (#61).
                "replayed": bool(payload.get("replayed")) or bool(entry.get("replayed")),
            },
        }
        self._done = _int(payload.get("done"))
        self._total = max(self._total, _int(payload.get("total")))
        # The RUN's spend, not this node's (``engine._emit_node`` sends
        # ``budget.tokens_spent``) — summary line only, see _summary_line.
        self._spend(payload.get("tokens"))

    def _absorb_items(self, payload: dict[str, Any]) -> None:
        node_id = str(payload.get("node_id") or "?")
        entry = self._entry(node_id)
        tokens = payload.get("tokens")
        self._nodes = {
            **self._nodes,
            node_id: {
                **entry,
                "items": (_int(payload.get("done")), _int(payload.get("total"))),
                "tokens": None if tokens is None else _int(tokens),
            },
        }
        self._spend(tokens)

    def _absorb_done(self, payload: dict[str, Any]) -> None:
        self.finished = True
        self.status = str(payload.get("status") or "")
        self.name = str(payload.get("name") or self.name)
        self._done = _int(payload.get("done"), self._done)
        self._total = max(self._total, _int(payload.get("total")))
        self._spend(payload.get("tokens"))

    def _spend(self, tokens: Any) -> None:
        """The summary's spend only ever climbs. Two sources feed it from two
        threads (node snapshots, item landings) and a cost that went DOWN on
        screen would read as a bug in the harness."""
        if tokens is not None:
            self._tokens = max(self._tokens, _int(tokens))

    def _fresh(self, node_type: str) -> dict[str, Any]:
        return {
            "type": node_type, "state": _PENDING, "items": None,
            "tokens": None, "replayed": False,
        }

    def _entry(self, node_id: str) -> dict[str, Any]:
        """A node the plan never named still gets a line — a newer engine must
        never be able to make an older block silently drop a node."""
        entry = self._nodes.get(node_id)
        if entry is None:
            entry = self._fresh("?")
            self._nodes = {**self._nodes, node_id: entry}
            self._order = self._order + (node_id,)
        return entry

    # --- pure: state -> frame ---------------------------------------------

    def compute_frame(self, width: int = 80, max_lines: int | None = None) -> list[str]:
        """The block as it should look right now. Pure — same state, same frame.

        ``max_lines`` clamps from the TOP (the summary is the one line that can
        never go): a frame taller than the screen is a cursor-up that clamps at
        the top of the terminal and repaints over the scrollback.
        """
        lines = [self._node_line(node_id) for node_id in self._order]
        lines.append(self._summary_line())
        if max_lines is not None:
            keep = max(1, _int(max_lines, 1))
            if len(lines) > keep:
                lines = lines[len(lines) - keep :]
        return [truncate(line, width) for line in lines]

    def _node_line(self, node_id: str) -> str:
        entry = self._nodes.get(node_id) or self._fresh("?")
        stranded = self.finished and str(entry.get("state")) in _UNSETTLED
        mark = "·" if stranded else _MARKS.get(str(entry.get("state")), "·")
        if entry.get("replayed") and not stranded:
            mark += f" {REPLAY_MARK}"  # served by the node cache, not a provider
        return f"{node_id} ({entry.get('type')}) {mark} {self._node_tail(entry, stranded)}"

    def _node_tail(self, entry: dict[str, Any], stranded: bool) -> str:
        items = entry.get("items")
        if items is None:
            if stranded:
                return _STOPPED
            return _STATE_WORDS.get(str(entry.get("state")), str(entry.get("state")))
        tail = f"items {items[0]}/{items[1]}"
        if entry.get("tokens") is not None:
            # The run's landed spend, kept on the fan-out line because that is
            # where the operator watches the cost climb item by item.
            tail += " · " + format_tokens(entry["tokens"])
        # ...and how far it GOT is worth keeping next to the fact that it stopped.
        return tail + " · " + _STOPPED if stranded else tail

    def _summary_line(self) -> str:
        parts: list[str] = []
        if self.finished and self.status:
            parts.append(self.status)
        parts.append(f"{self._done}/{self._total} nodes")
        parts.append(format_tokens(self._tokens))
        if self._faults:
            noun = "fault" if self._faults == 1 else "faults"
            parts.append(f"{self._faults} {noun}")
        return _RULE + " " + " · ".join(parts)


def _env_size(name: str) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError, TypeError):
        return 0


def terminal_size(stream: Any = None) -> tuple[int, int]:
    """(columns, lines) of the terminal ``stream`` is on, 80x24 when nobody can say.

    Deliberately NOT ``shutil.get_terminal_size``: that one probes
    ``sys.__stdout__``, and this module never writes to stdout. Under
    ``lohra chat --json > out.json`` stdout is a FILE while stderr is still the
    terminal being painted — probing stdout fails there and silently hands back
    80x24, so a narrower terminal gets lines wider than it can hold, which is
    the one thing the cursor arithmetic cannot survive.

    COLUMNS/LINES still win, per dimension: the override every tool honours.
    """
    columns, lines = _env_size("COLUMNS"), _env_size("LINES")
    if columns <= 0 or lines <= 0:
        probed = _probe_size(stream)
        columns = columns if columns > 0 else probed[0]
        lines = lines if lines > 0 else probed[1]
    return (max(1, columns), max(1, lines))


def _probe_size(stream: Any) -> tuple[int, int]:
    """Ask the stream's own fd. Anything at all goes wrong: 80x24, because a
    size query is never worth a turn."""
    try:
        size = os.get_terminal_size(stream.fileno())
        return (size.columns or 80, size.lines or 24)
    except Exception:
        return (80, 24)


class LivePainter:
    """The event sink that keeps ONE block alive on the terminal.

    Thin by contract: it moves the cursor, clears, and asks the block what to
    write. Everything it decides is arithmetic — how many lines are on screen,
    whether enough time passed to repaint, whether this event belongs to the run
    that currently owns the cursor.

    It takes a LOCK, and that is not optional: the sink is called from the run
    thread AND from the pipeline's worker threads, and two interleaved repaints
    would corrupt the cursor arithmetic the whole mode rests on.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
        size: Callable[[], tuple[int, int]] | None = None,
        min_interval: float = REDRAW_INTERVAL,
    ) -> None:
        self._stream = stream
        self._clock = clock
        # The size of the stream BEING PAINTED — never stdout's, which under
        # ``--json`` is a redirected file while this one is still a terminal.
        self._size = size or (lambda: terminal_size(stream))
        self._interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._block: LiveBlock | None = None
        self._painted = 0  # lines of ours currently on screen
        self._last_paint: float | None = None

    def __call__(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """The ``OnEvent`` sink. Never raises — same promise as ``write_lines``,
        for the same reason: the terminal is not the run's problem."""
        try:
            with self._lock:
                self._handle(str(run_id or ""), kind, payload)
        except Exception:  # a broken terminal is not a broken run
            logger.exception("workflow: live block painter failed for run %s", run_id)

    # --- internals (called under the lock) --------------------------------

    def _handle(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        if kind == PLAN:
            # A new run takes the cursor: whatever was there stays where it is
            # (v1 stacking) and the new block starts below the frozen one.
            self._freeze()
            write_lines(render_event(run_id, PLAN, payload), self._stream)
            block = LiveBlock(run_id)
            block.update(PLAN, payload)
            self._block = block
            self._paint()
            return

        block = self._block
        if block is None or block.run_id != run_id:
            # No live block, or an event from a run whose block is already
            # frozen: append it the plain way rather than lose it.
            self._insert_above(render_event(run_id, kind, payload))
            return

        block.update(kind, payload)
        if kind == FAULT:
            # Permanent: the text goes to the scrollback, the block comes back
            # underneath it carrying the count.
            self._insert_above(render_event(run_id, kind, payload))
            return
        if kind == DONE:
            # The last frame, then let go of the cursor: the agent's answer is
            # streamed right after this and must never be painted over.
            self._paint()
            self._freeze()
            return
        if self._due(kind, payload):
            self._paint()

    def _due(self, kind: str, payload: dict[str, Any]) -> bool:
        """Only an ``items`` BURST is worth coalescing.

        A ``node`` fires twice per node and a skipped repaint is never flushed
        later, so eating one would freeze a stale frame on screen for as long as
        the next leaf runs — five minutes of a lie to save one repaint. And an
        ``items`` at its WIDTH or its FINISH always paints, the same forced pair
        ``EventEmitter`` protects: a fan-out that ended must not read ``4/9``.
        """
        if kind != ITEMS or self._forced(payload):
            return True
        if self._last_paint is None:
            return True
        return (self._clock() - self._last_paint) >= self._interval

    def _forced(self, payload: dict[str, Any]) -> bool:
        done = _int(payload.get("done"))
        total = _int(payload.get("total"))
        return done <= 0 or (total > 0 and done >= total)

    def _paint(self) -> None:
        block = self._block
        if block is None:
            return
        columns, lines = self._size()
        # One line of headroom: the cursor itself lives on a line too.
        frame = block.compute_frame(width=columns, max_lines=max(1, lines - 1))
        self._erase()
        write_lines(frame, self._stream)
        self._painted = len(frame)
        self._last_paint = self._clock()

    def _insert_above(self, lines: list[str]) -> None:
        """Put permanent lines into the scrollback without losing the block."""
        self._erase()
        write_lines(lines, self._stream)
        self._paint()

    def _erase(self) -> None:
        if self._painted > 0:
            # Clear to END OF SCREEN, not per line: a frame that got SHORTER
            # would otherwise leave its old tail orphaned below the new one.
            self._raw(f"{CSI}{self._painted}A{CSI}0J")
        self._painted = 0

    def _freeze(self) -> None:
        """Leave what is on screen exactly where it is, and stop owning it."""
        self._block = None
        self._painted = 0
        self._last_paint = None

    def _raw(self, text: str) -> None:
        try:
            self._stream.write(text)
        except (UnicodeEncodeError, ValueError, OSError):
            return  # closed/detached/byte-limited: not the run's problem


def select_mode(*, isatty: bool, term: str | None = None, env: str | None = None) -> str:
    """Which live view this stream gets: ``fancy``, ``plain`` or ``off``.

    Append (``plain``) is the FALLBACK, deliberately: a pipe, a log file and a
    dumb terminal keep exactly the output they have today. ``LOHRA_LIVEVIEW``
    forces any of the three; anything else in it falls back to detection rather
    than erroring on a typo.
    """
    forced = (env or "").strip().lower()
    if forced in _MODES:
        return forced
    if isatty and (term or "").strip().lower() != "dumb":
        return FANCY
    return PLAIN
