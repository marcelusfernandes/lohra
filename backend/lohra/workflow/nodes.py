"""The workflow spec model — frozen dataclasses + the closed node-type registry.

The node-type set is CLOSED: the engine understands only these types, so an
authored spec can never express new control flow (that would be an engine
change, not a spec change). NODE_SPECS drives validation in schema.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lohra.agent.limits import MAX_AUTHORED_MAX_ITERATIONS
from lohra.workflow.artifact import BUILTIN_SCHEMAS


@dataclass(frozen=True)
class FieldSpec:
    name: str
    required: bool = False


@dataclass(frozen=True)
class NodeTypeSpec:
    """The allowed fields of one node type (drives the validator)."""

    type: str
    fields: tuple[FieldSpec, ...]

    def field_names(self) -> set[str]:
        return {f.name for f in self.fields}

    def required_names(self) -> set[str]:
        return {f.name for f in self.fields if f.required}


# Fields common to every node (besides id/type which the engine always reads).
# `label` and `phase` were REMOVED (issue #73): neither had a reader anywhere
# in the engine (no live view, progress, event or rollup consumed either), and
# the validator now refuses both didactically (schema.py) instead of accepting
# and ignoring them. A visual grouping will come with the interactive TUI.
_COMMON = (FieldSpec("required"), FieldSpec("depends_on"))

# Where a node's leaves RUN. Declared once and shared by every node type that
# spawns leaves of its own, so routing is one vocabulary instead of a per-type
# dialect: an `agent` and the rigor nodes (verify / judge_panel / loop_until_dry /
# gate / completeness_check) all take the same four. A rigor node resolves them
# ONCE and applies the result to EVERY leaf it spawns (all the skeptics, the
# attempts and their judges and the synthesis, every round, the draft and its
# reviewer) — without them "run this whole DAG on another provider" is
# unauthorable, because the rigor always falls back to the session's own model.
# Two fan-outs deliberately stay out, and BOTH still fall back to the session's
# model: pipeline STAGES (a stage is not a node) and ``parallel`` (its branches
# are prompts, not nodes). "This whole DAG elsewhere" therefore still has those
# two holes — routing them is a separate decision, not an oversight here.
_ROUTING = (
    FieldSpec("model"),
    FieldSpec("tier"),  # portable model choice (small|medium|big, WF-5)
    FieldSpec("effort"),
    FieldSpec("provider"),  # cross-provider leaf (different provider, same run)
)

# The routing vocabulary as plain names, so the two readers that ask "does this
# node declare a route?" — the cell identity (``strategies._ROUTING_FIELDS``)
# and the route answer of a ``route_fault`` pause (``route_fault.py``) — read it
# off the SAME declaration the validator does. Pinned by test against the copy
# in ``strategies``.
ROUTING_FIELDS = tuple(spec.name for spec in _ROUTING)

# The CLOSED node-type registry (spec §2.1). Adding control flow = adding here
# (an engine change), never something an authored spec can do.
NODE_SPECS: dict[str, NodeTypeSpec] = {
    "agent": NodeTypeSpec(
        "agent",
        _COMMON
        + _ROUTING
        + (
            FieldSpec("prompt", required=True),
            FieldSpec("schema"),
            FieldSpec("schema_ref"),
            FieldSpec("tool_less"),  # opt-in: force structured output (§5.2)
            FieldSpec("timeout"),  # seconds this leaf may take before it is cancelled
            FieldSpec("retries"),  # bounded same-route re-spawns (empty answer / dead leaf)
            FieldSpec("max_iterations"),  # tool rounds this leaf may take before it is cut off
        ),
    ),
    "parallel": NodeTypeSpec(
        "parallel",
        _COMMON
        + (
            FieldSpec("branches", required=True),
            # Opt-in, bounded fresh re-spawns of a DEAD branch (H7, #77). Same
            # syntax/validation as `agent.retries`, default 0 (unlike the
            # agent's 1: an author who never asks pays for exactly what #72
            # already did before this field existed).
            FieldSpec("retries"),
        ),
    ),
    "pipeline": NodeTypeSpec(
        "pipeline",
        _COMMON
        + (
            FieldSpec("items", required=True),
            FieldSpec("stages", required=True),
        ),
    ),
    "loop_until_dry": NodeTypeSpec(
        "loop_until_dry",
        _COMMON
        + _ROUTING
        + (
            FieldSpec("body", required=True),
            FieldSpec("stop_after_k_empty", required=True),
            FieldSpec("max_rounds", required=True),
            FieldSpec("budget"),
        ),
    ),
    "verify": NodeTypeSpec(
        "verify",
        _COMMON
        + _ROUTING
        + (
            FieldSpec("finding", required=True),
            FieldSpec("skeptics", required=True),
            FieldSpec("lenses"),
            FieldSpec("kill_if_majority_refute"),
        ),
    ),
    "judge_panel": NodeTypeSpec(
        "judge_panel",
        _COMMON
        + _ROUTING
        + (
            FieldSpec("attempts", required=True),
            FieldSpec("judges", required=True),
            FieldSpec("synthesize", required=True),
        ),
    ),
    "workflow": NodeTypeSpec(
        "workflow", _COMMON + (FieldSpec("ref", required=True), FieldSpec("args"))
    ),
    # Draft -> review -> revise, bounded (WF-6). ``body`` is agent-shaped;
    # ``validator`` is the prompt a reviewer leaf answers {ok, feedback} to.
    "gate": NodeTypeSpec(
        "gate",
        _COMMON
        + _ROUTING
        + (
            FieldSpec("body", required=True),
            FieldSpec("validator", required=True),
            FieldSpec("attempts"),
        ),
    ),
    # "What is still missing?" as a first-class node instead of a hand-rolled
    # agent + schema every time (spec §8 completeness critic).
    "completeness_check": NodeTypeSpec(
        "completeness_check",
        _COMMON + _ROUTING + (FieldSpec("task", required=True), FieldSpec("results", required=True)),
    ),
    # The human gate (WF-10): pauses the run until somebody answers — and, with
    # `accept`, until somebody answers YES (issue #74).
    "checkpoint": NodeTypeSpec(
        "checkpoint",
        _COMMON
        + (
            FieldSpec("prompt", required=True),
            FieldSpec("default"),
            FieldSpec("accept"),
            FieldSpec("on_reject"),
        ),
    ),
}

NODE_TYPES: frozenset[str] = frozenset(NODE_SPECS)

# Per-node lifecycle knobs (M4/WF-2). A leaf gets a deadline and a bounded number
# of fresh re-spawns; both are capped here so an authored spec can never ask for
# an unbounded leash (the same rule as the fan-out budget).
MAX_NODE_RETRIES = 3
DEFAULT_NODE_RETRIES = 1  # one re-spawn on an empty answer (WF-7)

# How many provider round-trips one leaf may take before the loop cuts it off.
# The same ceiling every model-authored surface answers to (delegate_task and
# spawn_session share it): an authored spec must never ask for an unbounded
# loop, which is a bill, not a leash. Above it the validator refuses
# didactically instead of clamping, so the author never runs under a leash they
# did not ask for.
MAX_NODE_MAX_ITERATIONS = MAX_AUTHORED_MAX_ITERATIONS

# What a workflow leaf gets when the node says nothing. Its own default, well
# above the tool-less chat default (8): a leaf WORKS — it reads, greps and
# writes — so it inherits the delegated-subagent cap. Pinned to
# ``delegate.CHILD_MAX_ITERATIONS`` by a test; kept as a literal rather than an
# import so the spec model does not depend on the tool layer.
DEFAULT_LEAF_MAX_ITERATIONS = 50

# A `gate` re-drafts until its validator approves. Bounded exactly like the
# fan-out budget: every extra attempt is another two leaves (body + review).
MAX_GATE_ATTEMPTS = 3
DEFAULT_GATE_ATTEMPTS = 2


def node_timeout(fields: dict[str, Any], default: float) -> float:
    """The node's ``timeout`` in seconds, or ``default``. Validation rejects a bad
    value at author time; this stays lenient so a stage dict can't crash a run."""
    value = fields.get("timeout")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return default
    return float(value)


def node_retries(fields: dict[str, Any], default: int = DEFAULT_NODE_RETRIES) -> int:
    """The node's ``retries`` (extra attempts), or ``default``. Capped, never
    negative — ``0`` is a valid opt-out and must survive the fallback."""
    value = fields.get("retries")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return min(value, MAX_NODE_RETRIES)


def node_max_iterations(fields: dict[str, Any], default: int) -> int:
    """The node's ``max_iterations``, or ``default``. Validation rejects a bad
    value at author time; this stays lenient so a stage dict — which no
    validator sees — can't crash a run, and capped so no path is unbounded."""
    value = fields.get("max_iterations")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return min(value, MAX_NODE_MAX_ITERATIONS)


def resolve_schema(schemas: dict[str, Any], fields: dict[str, Any]) -> dict | None:
    """An output schema from a fields dict: inline ``schema`` (a dict), a string
    ``schema`` that NAMES one (tolerate the schema/schema_ref mix-up), or
    ``schema_ref`` (looked up in the spec's named schemas).

    Pure and shared: the resolved schema goes INTO the cell hash, so the engine
    and anything recomputing that hash have to resolve it the same way. Lives
    here (the spec model) rather than on the engine so a reader without one can
    still ask the question.

    The RESERVED names (``artifact_manifest``/``artifact_manifests``, #45 E4)
    resolve without the author defining anything, and they WIN over ``schemas:``
    rather than losing to it: the validator already refuses a spec that
    redefines one, so the only way a local entry could shadow a builtin here is
    a spec dict nobody validated — exactly where the reserved meaning has to
    hold. A name that is neither still resolves to None, as it always did."""
    inline = fields.get("schema")
    if isinstance(inline, dict):
        return inline
    name = inline if isinstance(inline, str) else fields.get("schema_ref")
    if isinstance(name, str):  # schema: "name" — the common mix-up, coerced
        return BUILTIN_SCHEMAS.get(name) or schemas.get(name)
    return None


def gate_attempts(fields: dict[str, Any]) -> int:
    """A ``gate``'s attempt budget, capped. Validation rejects a bad value at
    author time; this stays lenient so no runtime path can crash on one."""
    value = fields.get("attempts")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_GATE_ATTEMPTS
    return min(value, MAX_GATE_ATTEMPTS)


# The node types whose OUTPUT is an AGGREGATION: a list whose top-level elements
# are one dead-able unit each. The noun is what a fault calls that element. Read
# by the fail-closed guard in ``prompts.first_aggregate_hole`` (issue #72);
# deliberately a CLOSED map, like the node-type registry itself.
#
# ``loop_until_dry`` is VACUOUS here today and kept only for that symmetry: a
# dead round is recorded as a fault and skipped (``strategies.run_loop_until_dry``
# never appends it), so its output cannot carry a top-level hole at all.
AGGREGATION_ELEMENT = {
    "parallel": "branch",
    "pipeline": "item",
    "loop_until_dry": "round",
}

# ...and the ones where a ``None`` element is NOT self-evidently a death, so the
# strategy must RECORD which elements really died. A ``pipeline`` stage may carry
# a schema whose ROOT permits null (``{"type": ["object", "null"]}``): that item
# settles ``None`` on a perfectly good answer, and inferring "dead item" from the
# value would diagnose a healthy pipeline as broken (issue #72, M1). A
# ``parallel`` branch is collected with NO schema, so there ``None`` IS the death.
AGGREGATION_RECORDS_DEATHS = frozenset({"pipeline"})

# What a `checkpoint` may do with an answer that is NOT in its `accept` list
# (issue #74). `fail` is the default and the fail-closed one: the node nulls and
# `required` decides whether the run stops. `pause` asks the same question again.
CHECKPOINT_ON_REJECT = ("fail", "pause")
DEFAULT_ON_REJECT = "fail"


def checkpoint_accepts(answer: Any, accept: Any) -> bool:
    """Does this human answer release the gate (issue #74)?

    ``.strip().lower()`` on both sides — the convention the rest of the harness
    already reads human words with (``launch``, ``route_fault``, ``sandbox``),
    never ``casefold``, so "SIM " and "sim" are one answer and nothing more
    exotic silently becomes one.

    Non-strings are compared through ``str()`` rather than refused: an answer is
    whatever the human sent, and ``checkpoint_answers`` carries any JSON value,
    so a dict or a bool must be judged rather than crash the gate. This is the
    ONE place the comparison lives: the runtime gate and ``cache_preview`` (which
    predicts what a resume will replay) read the same answer through it, so the
    preview can never promise a dependent will run on an answer the gate is
    about to refuse. A guarded gate has no ``default`` at all — the validator
    refuses the pair, because a default answers an UNATTENDED resume and a gate
    with ``accept`` exists precisely so a PERSON answers it."""
    if not isinstance(accept, (list, tuple)) or not accept:
        return True  # nothing declared: every answer is the output (WF-10)
    wanted = {str(item).strip().lower() for item in accept}
    return str(answer).strip().lower() in wanted


def checkpoint_on_reject(fields: dict[str, Any]) -> str:
    """A checkpoint's rejection policy, or the fail-closed default. Lenient like
    every other knob reader: validation refuses a bad value at author time."""
    value = fields.get("on_reject")
    return value if value in CHECKPOINT_ON_REJECT else DEFAULT_ON_REJECT


@dataclass(frozen=True)
class Node:
    """A validated node: id + type + the (frozen) authored fields."""

    id: str
    type: str
    fields: dict[str, Any] = field(default_factory=dict)

    @property
    def required(self) -> bool:
        return bool(self.fields.get("required", False))


@dataclass(frozen=True)
class WorkflowSpec:
    """A validated workflow: stable meta identity + schemas + the node DAG."""

    meta: dict[str, Any]
    inputs: dict[str, Any]
    schemas: dict[str, Any]
    nodes: tuple[Node, ...]

    @property
    def name(self) -> str:
        return str(self.meta.get("name", ""))

    def node(self, node_id: str) -> Node | None:
        return next((n for n in self.nodes if n.id == node_id), None)
