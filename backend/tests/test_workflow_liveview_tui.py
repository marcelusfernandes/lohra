"""Live view of a workflow run, redrawn IN PLACE (WF-31).

The append mode (``test_workflow_liveview.py``, 44 tests) stays exactly as it is
and stays the FALLBACK: a pipe, a CI log, a redirected stderr and a dumb terminal
all keep the scrollback they had. This file is about the other half the owner
asked for out loud — *"a contagem de runs pode subir conforme concluído e a
resposta completa vir na sequência"* — a status block that rewrites itself in
place, the way docker/npm/Claude Code do, and then freezes so the agent's own
answer lands underneath it instead of on top of it.

Two pieces, and the seam between them is the whole point:

- ``LiveBlock`` — the run's state and a **pure** ``compute_frame(width, max_lines)``
  that turns it into a list of strings. Everything interesting is tested here,
  with no terminal anywhere near it;
- ``LivePainter`` — a thin, dumb painter: cursor-up N, clear, rewrite. Tested
  against a fake buffer, asserting the literal escape sequences.

The bugs this file is written to keep out are the classic TUI ones: a line that
WRAPS or a block TALLER than the screen both break the cursor arithmetic and the
painter starts scribbling into the scrollback. So width and height are inputs to
a pure function, and both are pinned.

STDOUT DISCIPLINE, still and always: every byte here goes to stderr. Under
``lohra chat --json`` stdout is exactly one parseable object, block painter or not.
"""

import io
import re

import pytest

from lohra.workflow.events import DONE, FAULT, ITEMS, NODE, PLAN

ESC = "\x1b"
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class _Clock:
    """A hand-cranked monotonic clock — no sleeps, ever."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _plan(*nodes, run_id="8ebe0496aaaa", name="demo", budget=None):
    return {
        "run_id": run_id,
        "name": name,
        "token_budget": budget,
        "nodes": [{"id": nid, "type": ntype, "depends_on": []} for nid, ntype in nodes],
    }


def _block(*nodes, **kwargs):
    from lohra.workflow.liveview_tui import LiveBlock

    run_id = kwargs.get("run_id", "8ebe0496aaaa")
    block = LiveBlock(run_id)
    block.update(PLAN, _plan(*nodes, **kwargs))
    return block


def _painter(stream, *, clock=None, size=(80, 24), interval=0.0):
    from lohra.workflow.liveview_tui import LivePainter

    sizer = size if callable(size) else (lambda: size)
    return LivePainter(
        stream, clock=clock or _Clock(), size=sizer, min_interval=interval
    )


def _reset(buffer: io.StringIO) -> None:
    buffer.seek(0)
    buffer.truncate(0)


def _frame_lines(text: str) -> list[str]:
    """What a human would see: the escapes stripped, the blank lines dropped."""
    return [line for line in _ANSI.sub("", text).split("\n") if line]


# --- 1. LiveBlock.compute_frame: a pure function, state -> frames ----------


def test_the_frame_is_one_line_per_node_plus_a_summary():
    block = _block(("analises", "parallel"), ("parecer", "agent"))
    assert block.compute_frame(width=80) == [
        "analises (parallel) · waiting",
        "parecer (agent) · waiting",
        "─ 0/2 nodes · 0 tok",
    ]


def test_a_running_fan_out_shows_the_items_that_already_settled():
    block = _block(("analises", "parallel"), ("parecer", "agent"))
    block.update(
        NODE,
        {"node_id": "analises", "state": "running", "done": 0, "total": 2, "tokens": 0},
    )
    block.update(ITEMS, {"node_id": "analises", "done": 2, "total": 3, "tokens": 3000})
    assert block.compute_frame(width=80) == [
        "analises (parallel) ▸ items 2/3 · 3.0k tok",
        "parecer (agent) · waiting",
        "─ 0/2 nodes · 3.0k tok",
    ]


def test_a_settled_node_keeps_its_glyph_and_moves_the_summary():
    block = _block(("a", "agent"), ("b", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 2, "tokens": 8123}
    )
    frame = block.compute_frame(width=80)
    assert frame[0] == "a (agent) ✓ complete"
    assert frame[-1] == "─ 1/2 nodes · 8.1k tok"


def test_a_nulled_node_says_so_instead_of_quietly_looking_done():
    block = _block(("a", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "null", "done": 1, "total": 1, "tokens": 40}
    )
    assert block.compute_frame(width=80)[0] == "a (agent) ✗ null"


def test_the_runs_total_never_lands_on_a_plain_nodes_line():
    """``node`` events carry ``budget.tokens_spent`` — the RUN's spend, not the
    node's. On a per-node line that number reads as a per-node cost, which is a
    lie. It belongs to the summary line and nowhere else."""
    block = _block(("a", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 8123}
    )
    frame = block.compute_frame(width=80)
    assert "8.1k" not in frame[0]
    assert "8.1k tok" in frame[-1]


def test_the_fan_out_line_carries_the_runs_spend_not_a_per_node_cost():
    """``items`` events carry the run's landed tokens too (``engine.note_node_items``).
    The append mode already prints it on the fan-out line and the owner validated
    that as "o custo escalando" — so the block keeps it there, and the summary is
    fed from the same number. Recorded here so nobody later reads it as per-node."""
    block = _block(("fan", "parallel"))
    block.update(ITEMS, {"node_id": "fan", "done": 1, "total": 3, "tokens": 4100})
    frame = block.compute_frame(width=80)
    assert frame[0] == "fan (parallel) · items 1/3 · 4.1k tok"
    assert frame[-1] == "─ 0/1 nodes · 4.1k tok"


def test_a_fan_out_with_no_token_figure_still_renders():
    block = _block(("fan", "parallel"))
    block.update(ITEMS, {"node_id": "fan", "done": 0, "total": 3})
    assert block.compute_frame(width=80)[0] == "fan (parallel) · items 0/3"


def test_the_summary_spend_only_ever_climbs():
    """Two sources feed it (``node`` snapshots and ``items`` landings) and they
    arrive interleaved from different threads. A cost that went DOWN on screen
    would read as a bug in the harness."""
    block = _block(("a", "agent"), ("fan", "parallel"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 2, "tokens": 8123}
    )
    block.update(ITEMS, {"node_id": "fan", "done": 1, "total": 4, "tokens": 4000})
    assert block.compute_frame(width=80)[-1] == "─ 1/2 nodes · 8.1k tok"


def test_done_freezes_the_frame_with_the_final_status():
    block = _block(("a", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 500}
    )
    assert block.finished is False
    block.update(
        DONE,
        {"name": "demo", "status": "degraded", "done": 1, "total": 1, "tokens": 12300},
    )
    assert block.finished is True
    assert block.compute_frame(width=80)[-1] == "─ degraded · 1/1 nodes · 12.3k tok"


def test_a_run_that_stopped_never_leaves_a_node_reading_running():
    """The last frame is PERMANENT scrollback. A cancelled or faulted run settles
    no ``node`` event for what it was in the middle of, and a frozen line saying
    ``▸ running`` about a run that ended minutes ago is a lie nobody can correct."""
    block = _block(("a", "agent"), ("fan", "parallel"), ("z", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 3, "tokens": 90}
    )
    block.update(
        NODE, {"node_id": "fan", "state": "running", "done": 1, "total": 3, "tokens": 90}
    )
    block.update(ITEMS, {"node_id": "fan", "done": 2, "total": 5, "tokens": 900})
    block.update(
        DONE,
        {"name": "demo", "status": "cancelled", "done": 1, "total": 3, "tokens": 900},
    )
    assert block.compute_frame(width=80) == [
        "a (agent) ✓ complete",
        "fan (parallel) · items 2/5 · 900 tok · stopped",  # what it got to, and that it stopped
        "z (agent) · stopped",
        "─ cancelled · 1/3 nodes · 900 tok",
    ]


def test_a_run_that_finished_cleanly_says_nothing_about_stopping():
    block = _block(("a", "agent"))
    block.update(
        NODE, {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 90}
    )
    block.update(
        DONE, {"name": "demo", "status": "complete", "done": 1, "total": 1, "tokens": 90}
    )
    assert block.compute_frame(width=80)[0] == "a (agent) ✓ complete"


def test_a_fault_is_permanent_in_the_summary():
    """The fault TEXT goes to the scrollback (the painter's job). The block keeps
    the count, so a run that recovered still says out loud that it stumbled."""
    block = _block(("a", "agent"))
    block.update(FAULT, {"text": "bad: engine fault"})
    assert block.compute_frame(width=80)[-1] == "─ 0/1 nodes · 0 tok · 1 fault"
    block.update(FAULT, {"text": "worse: another"})
    assert block.compute_frame(width=80)[-1].endswith("· 2 faults")


def test_a_line_is_truncated_to_the_width_because_a_wrapped_line_breaks_the_cursor():
    block = _block(("um_no_com_um_nome_absurdamente_longo", "parallel"))
    block.update(ITEMS, {"node_id": "um_no_com_um_nome_absurdamente_longo",
                         "done": 2, "total": 9, "tokens": 91000})
    frame = block.compute_frame(width=20)
    assert all(len(line) <= 20 for line in frame)
    assert frame[0].endswith("…")


def test_an_absurd_width_still_yields_something_writable():
    block = _block(("a", "agent"))
    frame = block.compute_frame(width=1)
    assert all(len(line) <= 1 for line in frame)


def test_a_control_character_in_a_node_id_can_never_split_a_frame_line():
    """The schema only asks a node id to be a non-empty STRING, and the agent
    authors the spec: an id carrying a newline renders as two terminal rows
    while the painter counts one, and every cursor-up after it lands short."""
    block = _block(("a\nrm -rf /", "agent"))
    assert block.compute_frame(width=80) == [
        "a rm -rf / (agent) · waiting",
        "─ 0/1 nodes · 0 tok",
    ]


def test_an_escape_sequence_in_a_node_id_never_reaches_the_terminal():
    """A tab renders wider than it counts, an ESC moves the cursor itself."""
    line = _block(("a\x1b[2Jb\tc", "agent")).compute_frame(width=80)[0]
    assert line == "a [2Jb c (agent) · waiting"


def test_a_block_taller_than_the_terminal_is_clamped_and_keeps_the_summary():
    """The second classic TUI bug: ``ESC[40A`` on a 24-line screen clamps at the
    top and the next repaint scribbles over the scrollback. So the frame never
    gets taller than what was asked for — the oldest nodes go, the summary stays."""
    block = _block(*[(f"n{index}", "agent") for index in range(10)])
    frame = block.compute_frame(width=80, max_lines=4)
    assert len(frame) == 4
    assert frame[0] == "n7 (agent) · waiting"
    assert frame[-1].startswith("─")


def test_a_terminal_with_no_room_at_all_still_gets_the_summary():
    block = _block(("a", "agent"), ("b", "agent"))
    assert block.compute_frame(width=80, max_lines=1) == ["─ 0/2 nodes · 0 tok"]


def test_computing_a_frame_never_mutates_the_block():
    block = _block(("a", "agent"))
    first = block.compute_frame(width=80)
    assert block.compute_frame(width=80) == first


def test_an_unknown_kind_leaves_the_block_exactly_where_it_was():
    block = _block(("a", "agent"))
    before = block.compute_frame(width=80)
    block.update("something-new", {"whatever": 1})
    assert block.compute_frame(width=80) == before


def test_a_node_the_plan_never_named_still_gets_a_line():
    """A newer engine can emit a node the plan payload did not carry. An older
    block must show it, not drop it."""
    block = _block(("a", "agent"))
    block.update(
        NODE, {"node_id": "surpresa", "state": "running", "done": 0, "total": 2, "tokens": 0}
    )
    frame = block.compute_frame(width=80)
    assert frame[1] == "surpresa (?) ▸ running"


# --- 2. LivePainter: cursor-up, clear, rewrite -----------------------------


def test_the_plan_header_is_printed_once_above_the_block():
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent"), ("b", "agent")))
    written = buffer.getvalue()
    assert ESC not in written  # nothing on screen yet: nothing to move over
    assert "workflow demo (8ebe0496)" in written
    assert "  1. a (agent)" in written
    assert "a (agent) · waiting" in written
    assert written.endswith("─ 0/2 nodes · 0 tok\n")


def test_the_next_event_redraws_the_block_in_place():
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent"), ("b", "agent")))
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "running", "done": 0, "total": 2, "tokens": 0},
    )
    written = buffer.getvalue()
    # three lines were on screen (two nodes + summary): up three, clear, rewrite.
    assert written.startswith(f"{ESC}[3A{ESC}[0J")
    assert "a (agent) ▸ running" in written
    assert _frame_lines(written) == [
        "a (agent) ▸ running",
        "b (agent) · waiting",
        "─ 0/2 nodes · 0 tok",
    ]


def test_a_shrinking_frame_leaves_no_orphan_line_behind():
    """Clear-to-END-OF-SCREEN, not per-line erase: a frame that got SHORTER (the
    height clamp kicking in on a resize) must not leave the old tail on screen."""
    columns = {"cols": 80, "lines": 24}
    buffer = io.StringIO()
    painter = _painter(buffer, size=lambda: (columns["cols"], columns["lines"]))
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent"), ("b", "agent"), ("c", "agent")))
    _reset(buffer)
    columns["lines"] = 3  # the operator dragged the window shut
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "complete", "done": 1, "total": 3, "tokens": 10},
    )
    written = buffer.getvalue()
    assert written.startswith(f"{ESC}[4A{ESC}[0J")
    assert len(_frame_lines(written)) == 2


def test_the_painter_uses_the_new_width_after_a_resize():
    columns = {"cols": 80, "lines": 24}
    buffer = io.StringIO()
    painter = _painter(buffer, size=lambda: (columns["cols"], columns["lines"]))
    painter(
        "8ebe0496aaaa", PLAN, _plan(("um_no_com_um_nome_absurdamente_longo", "parallel"))
    )
    _reset(buffer)
    columns["cols"] = 18
    painter(
        "8ebe0496aaaa",
        NODE,
        {
            "node_id": "um_no_com_um_nome_absurdamente_longo",
            "state": "running",
            "done": 0,
            "total": 1,
            "tokens": 0,
        },
    )
    assert all(len(line) <= 18 for line in _frame_lines(buffer.getvalue()))


def test_the_painter_erases_exactly_the_rows_it_really_printed():
    """The count is the whole mode: a frame line that renders as TWO rows makes
    the next cursor-up clear the wrong region, permanently."""
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a\nrm -rf /", "agent")))
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a\nrm -rf /", "state": "running", "done": 0, "total": 1, "tokens": 0},
    )
    written = buffer.getvalue()
    assert written.startswith(f"{ESC}[2A{ESC}[0J")  # node line + summary
    assert len(_frame_lines(written)) == 2  # ...and that is what was on screen


def _fd_stream(fd: int) -> io.StringIO:
    class _Fd(io.StringIO):
        def fileno(self) -> int:
            return fd

    return _Fd()


def test_the_size_is_read_off_the_stream_being_painted_not_off_stdout(monkeypatch):
    """``lohra chat --json > out.json`` leaves stdout a FILE while stderr is
    still the terminal being painted. Probing stdout there silently hands back
    80x24, and every line is then written wider than the real terminal."""
    import os as _os

    from lohra.workflow import liveview_tui

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    monkeypatch.setattr(
        liveview_tui.os,
        "get_terminal_size",
        lambda fd: _os.terminal_size((20, 12) if fd == 7 else (80, 24)),
    )
    assert liveview_tui.terminal_size(_fd_stream(7)) == (20, 12)
    assert liveview_tui.terminal_size(io.StringIO()) == (80, 24)  # no fileno at all
    # ...and the painter asks about ITS stream by default, which is the point
    stream = _fd_stream(7)
    painter = liveview_tui.LivePainter(stream, clock=_Clock(), min_interval=0.0)
    painter("8ebe0496aaaa", PLAN, _plan(("um_no_com_um_nome_absurdamente_longo", "agent")))
    assert all(len(line) <= 20 for line in _frame_lines(stream.getvalue())[-2:])


def test_the_env_still_overrides_the_probe(monkeypatch):
    from lohra.workflow.liveview_tui import terminal_size

    monkeypatch.setenv("COLUMNS", "31")
    monkeypatch.setenv("LINES", "9")
    assert terminal_size(_fd_stream(7)) == (31, 9)


def test_a_fault_lands_in_the_scrollback_above_a_redrawn_block():
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    _reset(buffer)
    painter("8ebe0496aaaa", FAULT, {"text": "bad: engine fault"})
    written = buffer.getvalue()
    assert written.startswith(f"{ESC}[2A{ESC}[0J")
    lines = _frame_lines(written)
    # the fault text first (it stays), then the block, redrawn underneath it
    assert lines[0] == "[8ebe0496] ⚠ bad: engine fault"
    assert lines[1] == "a (agent) · waiting"
    assert lines[2].endswith("· 1 fault")


def test_the_final_frame_is_left_on_screen_and_never_redrawn_again():
    """DONE freezes: the agent's own answer is streamed right after this, and a
    painter still holding a cursor-up would scribble straight over it."""
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        DONE,
        {"name": "demo", "status": "complete", "done": 1, "total": 1, "tokens": 900},
    )
    written = buffer.getvalue()
    assert written.startswith(f"{ESC}[2A{ESC}[0J")
    assert _frame_lines(written)[-1] == "─ complete · 1/1 nodes · 900 tok"
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 900},
    )
    late = buffer.getvalue()
    assert ESC not in late  # nothing reaches back over the frozen frame
    assert late == "[8ebe0496] a ✓ · 1/1 nodes · 900 tok\n"


def test_a_second_run_freezes_the_first_block_and_stacks_below_it():
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    _reset(buffer)
    painter("bbbb2222cccc", PLAN, _plan(("z", "agent"), run_id="bbbb2222cccc", name="segundo"))
    written = buffer.getvalue()
    assert ESC not in written  # the first block is frozen where it is
    assert "workflow segundo (bbbb2222)" in written
    assert _frame_lines(written)[-1] == "─ 0/1 nodes · 0 tok"


def test_events_from_a_frozen_run_are_appended_above_the_live_block():
    """Two runs really do interleave. The older run's events must not be silently
    dropped — they land as append-mode lines above the block that owns the cursor."""
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    painter("bbbb2222cccc", PLAN, _plan(("z", "agent"), run_id="bbbb2222cccc", name="segundo"))
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 77},
    )
    written = buffer.getvalue()
    assert written.startswith(f"{ESC}[2A{ESC}[0J")
    lines = _frame_lines(written)
    assert lines[0] == "[8ebe0496] a ✓ · 1/1 nodes · 77 tok"
    assert lines[1] == "z (agent) · waiting"


def test_redraws_are_rate_limited_so_a_fast_fan_out_does_not_flicker():
    clock = _Clock(0.0)
    buffer = io.StringIO()
    painter = _painter(buffer, clock=clock, interval=0.1)
    painter("8ebe0496aaaa", PLAN, _plan(("fan", "parallel")))
    _reset(buffer)
    painter("8ebe0496aaaa", ITEMS, {"node_id": "fan", "done": 1, "total": 9, "tokens": 100})
    assert buffer.getvalue() == ""  # coalesced away
    clock.now = 0.5
    painter("8ebe0496aaaa", ITEMS, {"node_id": "fan", "done": 4, "total": 9, "tokens": 400})
    # the skipped event was not LOST: only its repaint was.
    assert "items 4/9 · 400 tok" in buffer.getvalue()


def test_a_node_transition_is_never_coalesced_away():
    """Only ``items`` bursts are worth coalescing. ``node`` fires twice per node
    and a skipped one is never flushed later — a fan-out settling just before a
    five-minute agent leaf would leave the pre-settle frame frozen on screen for
    those five minutes."""
    clock = _Clock(0.0)
    buffer = io.StringIO()
    painter = _painter(buffer, clock=clock, interval=1000.0)
    painter("8ebe0496aaaa", PLAN, _plan(("fan", "parallel"), ("lento", "agent")))
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "fan", "state": "complete", "done": 1, "total": 2, "tokens": 700},
    )
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "lento", "state": "running", "done": 1, "total": 2, "tokens": 700},
    )
    assert _frame_lines(buffer.getvalue())[-3:] == [
        "fan (parallel) ✓ complete",
        "lento (agent) ▸ running",
        "─ 1/2 nodes · 700 tok",
    ]


def test_a_fan_outs_width_and_finish_are_never_coalesced_away():
    """The same forced pair ``EventEmitter`` protects: the moment a fan-out's
    width is known, and the moment it is done. A limiter that ate either would
    leave the screen reporting ``2/9`` on a fan-out that has finished."""
    clock = _Clock(0.0)
    buffer = io.StringIO()
    painter = _painter(buffer, clock=clock, interval=1000.0)
    painter("8ebe0496aaaa", PLAN, _plan(("fan", "parallel")))
    _reset(buffer)
    painter("8ebe0496aaaa", ITEMS, {"node_id": "fan", "done": 0, "total": 9, "tokens": 0})
    assert "items 0/9" in buffer.getvalue()
    _reset(buffer)
    painter("8ebe0496aaaa", ITEMS, {"node_id": "fan", "done": 4, "total": 9, "tokens": 400})
    assert buffer.getvalue() == ""  # the middle of the burst: coalesced
    _reset(buffer)
    painter("8ebe0496aaaa", ITEMS, {"node_id": "fan", "done": 9, "total": 9, "tokens": 900})
    assert "items 9/9 · 900 tok" in buffer.getvalue()


def test_the_plan_the_fault_and_the_finish_are_never_rate_limited_away():
    clock = _Clock(0.0)
    buffer = io.StringIO()
    painter = _painter(buffer, clock=clock, interval=1000.0)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    _reset(buffer)
    painter("8ebe0496aaaa", FAULT, {"text": "bad: boom"})
    assert "bad: boom" in buffer.getvalue()
    _reset(buffer)
    painter(
        "8ebe0496aaaa",
        DONE,
        {"name": "demo", "status": "degraded", "done": 1, "total": 1, "tokens": 12300},
    )
    # ...and the fault it stumbled on is still counted in the frozen frame.
    assert _frame_lines(buffer.getvalue())[-1] == "─ degraded · 1/1 nodes · 12.3k tok · 1 fault"


def test_an_event_before_any_plan_is_appended_rather_than_dropped():
    buffer = io.StringIO()
    painter = _painter(buffer)
    painter(
        "8ebe0496aaaa",
        NODE,
        {"node_id": "a", "state": "running", "done": 0, "total": 1, "tokens": 0},
    )
    assert buffer.getvalue() == "[8ebe0496] a ▸ · 0/1 nodes · 0 tok\n"


def test_the_painter_never_takes_the_run_down_with_it():
    """Same contract as ``write_lines``: a progress line is never the thing that
    kills a turn — closed stream, detached stream, or a stream that is simply
    broken."""
    from lohra.workflow.liveview_tui import LivePainter

    class _Dead:
        def write(self, _text):
            raise OSError("terminal is gone")

        def flush(self):
            raise ValueError("closed")

    class _Wild:
        def write(self, _text):
            raise RuntimeError("some wrapper exploded")

        def flush(self):
            pass

    for stream in (_Dead(), _Wild()):
        painter = LivePainter(stream, clock=_Clock(), size=lambda: (80, 24), min_interval=0.0)
        painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
        painter(
            "8ebe0496aaaa",
            NODE,
            {"node_id": "a", "state": "complete", "done": 1, "total": 1, "tokens": 5},
        )
        painter("8ebe0496aaaa", DONE, {"name": "demo", "status": "complete",
                                       "done": 1, "total": 1, "tokens": 5})


def test_a_terminal_that_cannot_encode_the_glyphs_still_gets_the_block():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    painter = _painter(stream)
    painter("8ebe0496aaaa", PLAN, _plan(("a", "agent")))
    stream.flush()
    written = stream.buffer.getvalue().decode("ascii")
    assert "a (agent) - waiting" in written
    assert "- 0/1 nodes - 0 tok" in written


def test_the_folded_line_is_still_no_wider_than_the_terminal():
    """The ascii fold must not GROW a line past the width it was truncated to —
    a folded line that wraps breaks the very arithmetic the truncation protects."""
    from lohra.workflow.liveview import _ascii

    block = _block(("um_no_com_um_nome_absurdamente_longo", "parallel"), ("b", "agent"))
    block.update(FAULT, {"text": "boom"})
    block.update(ITEMS, {"node_id": "um_no_com_um_nome_absurdamente_longo",
                         "done": 2, "total": 9, "tokens": 91000})
    # every glyph the block can put on a line, settled states included
    for state in ("pending", "running", "complete", "null"):
        block.update(
            NODE,
            {"node_id": "b", "state": state, "done": 1, "total": 2, "tokens": 8123},
        )
        for line in block.compute_frame(width=24):
            assert len(_ascii(line)) <= len(line) <= 24


# --- 3. choosing a mode: append stays the fallback -------------------------


def test_a_pipe_gets_the_append_mode():
    from lohra.workflow.liveview_tui import PLAIN, select_mode

    assert select_mode(isatty=False, term="xterm-256color", env=None) == PLAIN


def test_a_dumb_terminal_gets_the_append_mode():
    from lohra.workflow.liveview_tui import PLAIN, select_mode

    assert select_mode(isatty=True, term="dumb", env=None) == PLAIN


def test_a_real_terminal_gets_the_block():
    from lohra.workflow.liveview_tui import FANCY, select_mode

    assert select_mode(isatty=True, term="xterm-256color", env=None) == FANCY
    assert select_mode(isatty=True, term=None, env=None) == FANCY


@pytest.mark.parametrize("value", ["plain", "fancy", "off", "  FANCY  "])
def test_the_env_forces_the_mode_whatever_the_terminal_says(value):
    from lohra.workflow.liveview_tui import select_mode

    assert select_mode(isatty=False, term="dumb", env=value) == value.strip().lower()


def test_an_unknown_env_value_falls_back_to_detection_instead_of_erroring():
    from lohra.workflow.liveview_tui import FANCY, PLAIN, select_mode

    assert select_mode(isatty=True, term="xterm", env="banana") == FANCY
    assert select_mode(isatty=False, term="xterm", env="") == PLAIN


# --- 4. the CLI picks the sink ---------------------------------------------


class _FakeTTY(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def test_the_cli_falls_back_to_the_append_sink_off_a_terminal(monkeypatch):
    from lohra import cli
    from lohra.workflow.liveview_tui import LivePainter

    monkeypatch.delenv("LOHRA_LIVEVIEW", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    sink = cli._live_workflow_view(_FakeTTY(False))
    assert sink is not None and not isinstance(sink, LivePainter)


def test_the_cli_paints_a_block_on_a_real_terminal(monkeypatch):
    from lohra import cli
    from lohra.workflow.liveview_tui import LivePainter

    monkeypatch.delenv("LOHRA_LIVEVIEW", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert isinstance(cli._live_workflow_view(_FakeTTY(True)), LivePainter)


def test_the_env_can_switch_the_cli_sink_either_way(monkeypatch):
    from lohra import cli
    from lohra.workflow.liveview_tui import LivePainter

    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("LOHRA_LIVEVIEW", "fancy")
    assert isinstance(cli._live_workflow_view(_FakeTTY(False)), LivePainter)
    monkeypatch.setenv("LOHRA_LIVEVIEW", "plain")
    assert not isinstance(cli._live_workflow_view(_FakeTTY(True)), LivePainter)


def test_off_means_no_sink_is_attached_at_all(monkeypatch):
    from lohra import cli

    monkeypatch.setenv("LOHRA_LIVEVIEW", "off")
    assert cli._live_workflow_view(_FakeTTY(True)) is None


def test_a_stream_that_cannot_answer_isatty_is_treated_as_a_pipe(monkeypatch):
    """Some wrappers raise on ``isatty``. Guessing "terminal" there would paint
    escapes into a log file."""
    from lohra import cli
    from lohra.workflow.liveview_tui import LivePainter

    monkeypatch.delenv("LOHRA_LIVEVIEW", raising=False)

    class _Rude(io.StringIO):
        def isatty(self):
            raise ValueError("detached")

    assert not isinstance(cli._live_workflow_view(_Rude()), LivePainter)


def test_json_stdout_is_still_exactly_one_object_with_the_block_painter_on(
    monkeypatch, tmp_path, capsys
):
    """THE contract, re-pinned for the new mode: the painter writes ANSI to
    stderr and stdout stays exactly one parseable envelope."""
    import json

    from lohra import cli
    from tests.test_workflow_liveview import _patch_workflow_client

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_LIVEVIEW", "fancy")  # forced: capsys stderr is no tty
    _patch_workflow_client(monkeypatch)
    code = cli.run_chat("roda um workflow", provider="anthropic", json_output=True)
    captured = capsys.readouterr()
    assert code == 0
    envelope = json.loads(captured.out)
    assert envelope["input"] == "roda um workflow"
    assert ESC not in captured.out  # not one escape byte in the envelope stream
    # ...and the BLOCK really was the mode chosen: the plan header is emitted
    # synchronously inside ``start``, and the frame right under it is a line the
    # append mode never produces. (The cursor moves themselves come from the run
    # thread's later repaints, which is why they are not asserted here.)
    assert "workflow chatty (" in captured.err
    assert "a (agent) · waiting" in captured.err
    assert "─ 0/1 nodes · 0 tok" in captured.err


def test_the_live_view_can_be_silenced_entirely(monkeypatch, tmp_path, capsys):
    from lohra import cli
    from tests.test_workflow_liveview import _patch_workflow_client

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_LIVEVIEW", "off")
    _patch_workflow_client(monkeypatch)
    assert cli.run_chat("roda um workflow", provider="anthropic") == 0
    captured = capsys.readouterr()
    assert "workflow chatty (" not in captured.err
    assert "workflow chatty (" not in captured.out
