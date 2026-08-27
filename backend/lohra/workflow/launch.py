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

from typing import Any

from lohra.workflow.gates import CHECKPOINT
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

def launch_args(
    args: dict | None, resume_run_id: str | None, prior: DurableRun | None
) -> dict:
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
    would re-pause on the same node and read as "the resume did nothing"."""
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
        f"and is waiting for an answer: {pending.get('prompt', '')}\n"
        f'    e.g. checkpoint_answers: {{"{node_id}": "<your answer>"}}'
    )
