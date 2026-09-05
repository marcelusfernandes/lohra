"""What a launch actually runs — resolving spec, args and pending answers.

Pure by construction: every one of these is a function of what the caller sent
plus what the run's own line already knows (``DurableRun``), never of the
service's live state. They were methods on ``WorkflowService`` that touched no
``self`` at all — here they are testable alone, and the service keeps only the
branch points.

The shared rule, in one sentence: **explicit always wins, persisted is the
fallback**. A resume that sends nothing replays what the run was launched with
(spec, args) and is held to the question it paused on (a checkpoint); a resume
that sends a spec is a new instruction, so the old run's pending question is
moot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.nodes import NODE_SPECS, ROUTING_FIELDS
from lohra.workflow.route_fault import (
    ROUTE_ABORT,
    ROUTE_FAULT,
    looks_like_route_answer,
    nested_route_refusal,
    parse_route_answer,
    route_label,
    same_dead_route,
)
from lohra.workflow.runstate_store import DurableRun


def launch_spec(
    spec_dict: Any, resume_run_id: str | None, prior: DurableRun | None
) -> tuple[Any, str | None]:
    """(spec, error): the spec this launch runs, else why there is none.

    A resume replays the spec the run already persisted — exactly what the
    quota auto-resume has always done, now reachable from the tool too, so
    ``run_workflow(resume_run_id=...)`` means what its own guidance says.
    An explicit spec always wins: the persisted copy is a fallback, never an
    override."""
    if spec_dict is not None:
        return spec_dict, None
    if not resume_run_id:
        return None, "run_workflow needs a 'spec' object (with meta + nodes)"
    if prior is None or prior.spec is None:
        return None, (
            f"no spec on file for workflow run {resume_run_id!r} — pass "
            "'spec' explicitly (nothing on disk names this run)"
        )
    return prior.spec, None


def launch_args(args: dict | None, resume_run_id: str | None, prior: DurableRun | None) -> dict:
    """The inputs this launch runs with — the ``launch_spec`` rule, for args.

    A resume that sends none replays the ones the run persisted: the spec it
    is replaying still references ``${args.x}``, and starting the resumed
    stretch with an empty mapping resolves every one of them to null (WF-24).
    Explicit args always win, ``{}`` included — clearing the inputs is a
    thing a caller may mean; omitting the field is not.
    """
    if args is not None:
        return args
    if not resume_run_id:
        return {}
    return dict(prior.args) if prior is not None and prior.args else {}


def checkpoint_answers(
    resume_run_id: str | None,
    answers: Any,
    explicit_spec: bool,
    prior: DurableRun | None,
) -> tuple[dict, str | None]:
    """(answers, error) for this launch — filling in a declared default (WF-10).

    Only a PURE resume is held to the pending question: re-sending a spec
    means "run THIS", which makes the old run's checkpoint moot (if the new
    spec still hits one, it pauses on its own).

    A pending checkpoint with a ``default`` is answered here rather than in
    the engine, so the engine only ever knows one concept — an answer. With
    neither an answer nor a default, refusing is the honest reply: launching
    would re-pause on the same node and read as "the resume did nothing".

    The ``node_id`` this reads is the KEY the pause asked under, which for a
    gate inside a nested template is ``sub[<workflow node id>]:<id>`` (#78) —
    keyed by the CALL, so two nodes running one template ask separately.
    Answering a nested gate with the bare id therefore lands in this refusal —
    didactically, naming the namespaced key — instead of opening the parent's
    gate of the same name."""
    resolved = dict(answers) if isinstance(answers, dict) else {}
    if explicit_spec or not resume_run_id:
        return resolved, None
    if prior is None or prior.status != "paused" or prior.pause_reason != CHECKPOINT:
        return resolved, None
    pending = prior.checkpoint or {}
    node_id = pending.get("node_id")
    if not node_id or node_id in resolved:
        return resolved, None
    if "default" in pending:  # `in`, not .get(): a null default is a default
        resolved[node_id] = pending["default"]
        return resolved, None
    return resolved, (
        f"workflow run {resume_run_id!r} is paused at checkpoint {node_id!r} "
        f"and is waiting for an answer from a HUMAN: {pending.get('prompt', '')}\n"
        "Ask the human and pass their answer verbatim; do not infer or author one.\n"
        f'    checkpoint_answers: {{"{node_id}": "<human answer verbatim>"}}'
    )


@dataclass(frozen=True)
class RouteLaunch:
    """What a launch does with a ``route_fault`` answer, decided before anything
    is acquired, spawned or written.

    Exactly one of the three is ever set: ``error`` (a didactic refusal),
    ``abort_node`` (the human said stop), or ``node_id``+``route`` (re-route that
    node in the persisted spec). ``answers`` is what still belongs to the
    CHECKPOINT channel — the route answer is stripped out, because everything
    downstream (``preview_resume``, ``engine.checkpoint_answers``) reads that
    mapping as "answers for checkpoint nodes" and a stray routing key would
    travel through both as an answer nobody asked for."""

    answers: dict = field(default_factory=dict)
    error: str | None = None
    abort_node: str | None = None
    node_id: str | None = None
    route: dict | None = None


def _node_type(prior: DurableRun | None, node_id: Any) -> str | None:
    """The type the persisted spec gives this node, or None if it names none."""
    spec = prior.spec if prior is not None else None
    nodes = spec.get("nodes") if isinstance(spec, dict) else None
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if isinstance(node, dict) and node.get("id") == node_id:
            return str(node.get("type") or "") or None
    return None


def _reads_as_route_answer(answer: Any, node_type: str | None) -> bool:
    """Would this answer be read as a ROUTE answer on a run paused at
    ``route_fault``? (Used only to refuse one sent at the wrong moment.)

    Gated on the TARGET's node type, and specifically on "does this type take
    routing at all" rather than on "is it a checkpoint". A checkpoint one level
    down inside a nested template is answered under a NAMESPACED key
    (``sub[<workflow node>]:<id>``, #78 — the shape a nested route already had,
    keyed by the CALL), which is in no spec this run persists, so its answers
    reach here with no type the
    parent spec can name — and a human answering such a gate with "abort", or
    with an object that happens to carry a ``model`` key, must not have their
    answer refused as a misplaced route. Unknown type => not a route answer.
    """
    routed = NODE_SPECS.get(node_type or "")
    if routed is None or not set(ROUTING_FIELDS) <= routed.field_names():
        return False
    return looks_like_route_answer(answer) or (
        isinstance(answer, str) and answer.strip().lower() == ROUTE_ABORT
    )


def route_answer(
    resume_run_id: str | None,
    answers: Any,
    explicit_spec: bool,
    prior: DurableRun | None,
) -> RouteLaunch:
    """Read a ``route_fault`` answer out of ``checkpoint_answers`` (decisão 1 do
    dono, #43) — or say, didactically, why this one is not one.

    Pure, like everything else here: a launch decision made from what the caller
    sent plus the run's own line. The ORDER is part of the contract, pinned by
    test, and three steps of it are load-bearing:

    - "one channel at a time" is decided BEFORE the answer is parsed, so a
      caller who sent both gets told which to drop, not which key is misspelled;
    - ``abort`` is read BEFORE the nested-template refusal: an abort edits
      nothing, so "the route lives in a template" is the wrong remedy for a
      human who asked to STOP — a nested route cannot be answered with a route,
      but it can always be answered with a cancel;
    - the nested refusal still comes before the node is looked up, since the
      namespaced ``sub[ref]:node`` id is in no spec and the not-found message
      would mask the real reason.

    A ``checkpoint`` pause is untouched: its answers keep meaning exactly what
    they meant, "abort" included. So is a FRESH launch — this whole decision is
    about a run that already stopped, and a launch with no ``resume_run_id`` has
    no pause to answer."""
    resolved = dict(answers) if isinstance(answers, dict) else {}
    if not resume_run_id:
        return RouteLaunch(answers=resolved)
    paused_on_route = (
        prior is not None
        and prior.status == "paused"
        and prior.pause_reason == ROUTE_FAULT
    )
    if not paused_on_route:
        # An answer that reads as a ROUTE answer — a routing object, or the word
        # ``abort`` — aimed at a node that takes routing can only be a route
        # answer sent at the wrong moment. Both halves are refused, and the
        # ABORT half is the one that had teeth: after the first abort the run is
        # ``cancelled``, a resume of a cancelled run is allowed by design, and a
        # repeated ``{"target": "abort"}`` used to sail through as an ordinary
        # checkpoint answer — relaunching the run, re-spawning the route that
        # was already known to be dead, and leaving the line saying "the run was
        # cancelled" over a run that is paused again. A cancel is not a thing
        # you can say twice into a resume.
        for node_id, answer in resolved.items():
            if not _reads_as_route_answer(answer, _node_type(prior, node_id)):
                continue
            where = (
                f"is {prior.status}"
                + (f" ({prior.pause_reason})" if prior.pause_reason else "")
                if prior is not None
                else "is not on file"
            )
            names = (
                'is the word "abort"' if isinstance(answer, str) else "names a route"
            )
            return RouteLaunch(
                answers=resolved,
                error=(
                    f"the answer for {node_id!r} {names}, but workflow run "
                    f"{resume_run_id!r} {where} — a route answer is only read on a "
                    "run PAUSED with reason 'route_fault', so nothing was "
                    "launched (a resume that carried it would have run the spec "
                    "on file unchanged). Move a route on any other run with an "
                    "explicit adapted spec, and stop a run with workflow_cancel."
                ),
            )
        return RouteLaunch(answers=resolved)

    payload = prior.route_fault or {}
    dead = payload.get("node_id")
    if not dead:
        if not resolved:
            return RouteLaunch(answers=resolved)
        return RouteLaunch(
            answers=resolved,
            error=(
                f"workflow run {resume_run_id!r} is paused on a dead route whose "
                "payload names no node, so there is nothing an answer can move — "
                "resume it with an explicit adapted spec."
            ),
        )
    strangers = sorted(str(key) for key in resolved if key != dead)
    if strangers:
        return RouteLaunch(
            answers=resolved,
            error=(
                f"workflow run {resume_run_id!r} is paused on the dead route of "
                f"node {dead!r}; {', '.join(strangers)} is not that node. While a "
                "run is paused on a route, the only answer it reads is the one "
                "for the node that died (answers already given to checkpoint "
                "nodes are cached — they never need re-sending)."
            ),
        )
    if dead not in resolved:
        return RouteLaunch(answers=resolved)  # a plain resume: unchanged behaviour
    if explicit_spec:
        # Contradictory instructions, refused rather than ranked: a spec says
        # "run THIS" and an answer says which route (or whether) the run
        # continues on. Refusing costs nothing — the run stays paused and the
        # caller resends the one they meant.
        return RouteLaunch(
            answers=resolved,
            error=(
                f"one channel per resume: workflow run {resume_run_id!r} got BOTH "
                f"an explicit spec and an answer for {dead!r}. Send the adapted "
                "spec alone, or the answer alone (it acts on the spec already on "
                "file) — never both, because only one of them can be the last "
                "word on where this run goes."
            ),
        )
    parsed = parse_route_answer(resolved[dead])
    if isinstance(parsed, str):
        return RouteLaunch(answers=resolved, error=f"node {dead!r}: {parsed}")
    if parsed.abort:
        # BEFORE the nested refusal, deliberately. An abort edits nothing, so
        # "the route lives in a template" is the wrong remedy for a human who
        # asked to STOP: a nested route cannot be answered with a route, but it
        # can always be answered with a cancel.
        return RouteLaunch(abort_node=str(dead))
    if payload.get("template"):
        return RouteLaunch(
            answers=resolved, error=nested_route_refusal(payload["template"])
        )
    if same_dead_route(parsed.route, payload):
        return RouteLaunch(
            answers=resolved,
            error=(
                f"node {dead!r}: {route_label(payload.get('provider'), payload.get('model'))} "
                "is the route that just died — answering with it again would re-pay "
                "the node to fail the same way. Name a different provider/model, or "
                'answer "abort".'
            ),
        )
    return RouteLaunch(
        answers={key: value for key, value in resolved.items() if key != dead},
        node_id=str(dead),
        route=dict(parsed.route),
    )
