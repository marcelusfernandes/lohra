"""Validate an authored workflow spec BEFORE any spawn (spec §2, Milestone A).

``validate_spec`` returns either a ``WorkflowSpec`` or a ``ValidationError`` — it
NEVER raises. Errors are didactic: each carries the node id, field, the rule
broken, and a corrected example, so the agent can fix its own spec.

Checks: structure; closed node-type set; required/unknown fields; ``schema`` xor
``schema_ref`` and ``schema_ref`` resolves; reference grammar (no expressions);
reference targets exist; ``depends_on`` is a list of existing node ids; no
dependency cycles; static-literal fan-out under cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lohra.workflow import refs
from lohra.workflow.artifact import RESERVED_SCHEMA_NAMES
from lohra.workflow.nodes import (
    MAX_GATE_ATTEMPTS,
    MAX_NODE_MAX_ITERATIONS,
    MAX_NODE_RETRIES,
    NODE_SPECS,
    NODE_TYPES,
    Node,
    WorkflowSpec,
)
from lohra.workflow.tiers import MODEL_TIERS

# A literal fan-out (a `branches`/`items` list authored inline) over this many
# entries is rejected at validation time. Dynamic (${ref}) fan-out is bounded at
# RUNTIME by the unified budget (spec §7.1-7.2); this is only the static guard.
MAX_STATIC_FANOUT = 64

# Reference roots that are not node ids but engine-provided context, valid inside
# the relevant node templates: args (run inputs), item/stage (pipeline), winner
# (judge_panel synthesize), round/so_far (loop_until_dry body).
_NON_NODE_ROOTS = frozenset({"args", "item", "stage", "winner", "round", "so_far"})


@dataclass(frozen=True)
class SpecIssue:
    rule: str
    message: str
    node_id: str | None = None
    field: str | None = None
    example: str | None = None


@dataclass(frozen=True)
class ValidationError:
    issues: tuple[SpecIssue, ...] = field(default_factory=tuple)

    @property
    def message(self) -> str:
        """One line per issue, with its corrected example indented under it.

        The example is the whole point of a didactic error — an issue that
        carries one must SHOW it, or the author has to guess the fix."""
        return "\n".join(_render_issue(i) for i in self.issues)


def _render_issue(issue: SpecIssue) -> str:
    head = (
        f"[{issue.rule}]{' ' + issue.node_id if issue.node_id else ''}"
        f"{' .' + issue.field if issue.field else ''}: {issue.message}"
    )
    if not issue.example:
        return head
    first, *rest = issue.example.splitlines() or [""]
    lines = [f"    e.g. {first}"] + [f"    {line}" for line in rest]
    return "\n".join([head, *lines])


def _iter_strings(value: Any) -> list[str]:
    """Every string anywhere inside an authored value (for ref scanning)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for item in value for s in _iter_strings(item)]
    if isinstance(value, dict):
        return [s for item in value.values() for s in _iter_strings(item)]
    return []


def _ref_roots(value: Any) -> set[str]:
    """First path segment of every valid reference inside ``value``."""
    roots: set[str] = set()
    for text in _iter_strings(value):
        for inner in refs.find_refs(text):
            if refs.is_valid_ref(inner):
                roots.add(inner.split(".")[0])
    return roots


def _validate_meta(raw_meta: Any, issues: list[SpecIssue]) -> dict[str, Any]:
    if not isinstance(raw_meta, dict):
        issues.append(SpecIssue("meta", "meta must be a mapping with at least a name", field="meta"))
        return {}
    if not raw_meta.get("name") or not isinstance(raw_meta.get("name"), str):
        issues.append(
            SpecIssue("meta", "meta.name is required and must be a string", field="meta.name",
                      example='meta:\n  name: triage-bugs')
        )
    for text in _iter_strings(raw_meta):
        if refs.find_refs(text):
            issues.append(
                SpecIssue("meta", "meta must be pure literals — no ${references}", field="meta")
            )
            break
    return raw_meta


def _validate_schemas(raw_schemas: Any, issues: list[SpecIssue]) -> dict[str, Any]:
    if not isinstance(raw_schemas, dict):
        issues.append(SpecIssue("schemas", "schemas must be a mapping of name -> JSON-Schema",
                                field="schemas"))
        return {}
    for name, definition in raw_schemas.items():
        if name in RESERVED_SCHEMA_NAMES:
            # The harness MEASURES what a cell under this name declares (#45 E4),
            # so the name has to mean one shape everywhere. Redefining it would
            # leave the store measuring paths the author never promised — refuse
            # instead of silently preferring one of the two definitions.
            issues.append(
                SpecIssue("schema_reserved",
                          f"schema name {name!r} is reserved by the harness — reference it with "
                          "schema_ref and define nothing", field=f"schemas.{name}",
                          example="schema_ref: artifact_manifest")
            )
            continue
        if not isinstance(definition, dict):
            issues.append(
                SpecIssue("schema_def", f"schema {name!r} must be a JSON-Schema object",
                          field=f"schemas.{name}", example='schemas:\n  VERDICT: {type: object}')
            )
    return raw_schemas


def _validate_node_shape(
    raw: Any, index: int, issues: list[SpecIssue], supported: frozenset[str] | None
) -> Node | None:
    if not isinstance(raw, dict):
        issues.append(SpecIssue("node", f"node #{index} must be a mapping", field=f"nodes[{index}]"))
        return None
    node_id = raw.get("id")
    node_type = raw.get("type")
    if not node_id or not isinstance(node_id, str):
        issues.append(SpecIssue("node_id", f"node #{index} needs a string 'id'",
                                field=f"nodes[{index}].id", example="- id: scan"))
        return None
    if node_type not in NODE_TYPES:
        issues.append(
            SpecIssue("node_type", f"unknown node type {node_type!r}",
                      node_id=node_id, field="type",
                      example=f"type: one of {sorted(NODE_TYPES)}")
        )
        return None
    if supported is not None and node_type not in supported:
        issues.append(
            SpecIssue("unsupported_type",
                      f"node type {node_type!r} is valid but not executable yet",
                      node_id=node_id, field="type",
                      example=f"supported now: {sorted(supported)}")
        )
        return None
    spec = NODE_SPECS[node_type]
    fields = {k: v for k, v in raw.items() if k not in ("id", "type")}
    allowed = spec.field_names()
    for key in fields:
        if key == "min_success_ratio":
            # issue #15: removed, not merely unknown — a generic "no field"
            # message would send the author hunting for the right name when
            # there ISN'T one; name the removal and the real substitute.
            issues.append(
                SpecIssue(
                    "min_success_ratio_removed",
                    "'min_success_ratio' was removed: the engine never enforced "
                    "it and the spec left its semantics ambiguous (what the "
                    "failure marker is, what 'completed' means per node-type, "
                    "how it interacts with resume cache). Use a 'gate' or "
                    "'completeness_check' node marked required: true that reads "
                    "the fan-out result instead.",
                    node_id=node_id,
                    field="min_success_ratio",
                    example=(
                        "- id: check_fanout\n"
                        "  type: gate\n"
                        "  required: true\n"
                        '  body: {prompt: "Summarize ${fanout}"}\n'
                        '  validator: "Did every item in ${fanout} succeed?"'
                    ),
                )
            )
            continue
        if key not in allowed:
            issues.append(
                SpecIssue("unknown_field", f"{node_type!r} has no field {key!r}",
                          node_id=node_id, field=key, example=f"allowed: {sorted(allowed)}")
            )
    for required in spec.required_names():
        if required not in fields:
            issues.append(
                SpecIssue("missing_field", f"{node_type!r} requires {required!r}",
                          node_id=node_id, field=required)
            )
    if node_type == "agent" and "schema" in fields and "schema_ref" in fields:
        issues.append(
            SpecIssue("schema_xor", "use either 'schema' or 'schema_ref', not both",
                      node_id=node_id, field="schema_ref")
        )
    return Node(id=node_id, type=node_type, fields=fields)


def _validate_references(
    node: Node, node_ids: set[str], schemas: dict[str, Any], issues: list[SpecIssue]
) -> None:
    for text in _iter_strings(node.fields):
        for bad in refs.invalid_refs(text):
            issues.append(
                SpecIssue("ref_expression", f"reference ${{{bad}}} is not a plain path "
                          "(no expressions/arithmetic/calls)", node_id=node.id,
                          example="${scan.ids} or ${args.dump}")
            )
        for inner in refs.find_refs(text):
            if not refs.is_valid_ref(inner):
                continue
            root = inner.split(".")[0]
            if root not in _NON_NODE_ROOTS and root not in node_ids:
                issues.append(
                    SpecIssue("ref_target", f"reference ${{{inner}}} points at unknown node "
                              f"{root!r}", node_id=node.id, example="reference an existing node id")
                )
    # A RESERVED name resolves with no ``schemas:`` entry at all (#45 E4) — it is
    # the harness's own shape, so demanding the author declare it would make the
    # one schema they must NOT write the one the validator insists on.
    schema_ref = node.fields.get("schema_ref")
    if isinstance(schema_ref, str) and schema_ref not in schemas:
        if schema_ref not in RESERVED_SCHEMA_NAMES:
            issues.append(
                SpecIssue("schema_ref",
                          f"schema_ref {schema_ref!r} has no matching entry in schemas:",
                          node_id=node.id, field="schema_ref")
            )
    # 'schema' should be an inline object. A STRING is the common schema/schema_ref
    # mix-up — tolerated at runtime IF it names a known schema, but an unresolvable
    # string (or any non-dict) silently means "no validation", so catch it here.
    schema = node.fields.get("schema")
    if schema is not None and not isinstance(schema, dict):
        if not (isinstance(schema, str) and (schema in schemas or schema in RESERVED_SCHEMA_NAMES)):
            issues.append(
                SpecIssue("schema_type", "'schema' must be a JSON-Schema object; to "
                          "reference a named schema use 'schema_ref'", node_id=node.id,
                          field="schema", example="schema_ref: my_schema")
            )


def _validate_lifecycle(node: Node, issues: list[SpecIssue]) -> None:
    """``required``/``timeout``/``retries``/``max_iterations`` must be real,
    bounded values.

    A declared-but-ignored knob is the footgun this catches: the runtime falls
    back to the default on garbage, so without an author-time error the spec
    would silently run under a leash it never asked for.
    """
    if "required" in node.fields and not isinstance(node.fields["required"], bool):
        # ``required: "no"`` is TRUTHY. Since issue #15 this field really aborts
        # a run, so the string that means the opposite of what it says is now a
        # spec that stops on its first null instead of a typo nobody noticed.
        issues.append(
            SpecIssue(
                "field_value",
                "'required' must be true or false",
                node_id=node.id,
                field="required",
                example="required: true",
            )
        )
    if "timeout" in node.fields:
        value = node.fields["timeout"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            issues.append(
                SpecIssue(
                    "field_value",
                    "'timeout' must be a positive number of seconds",
                    node_id=node.id,
                    field="timeout",
                    example="timeout: 120",
                )
            )
    if "retries" in node.fields:
        value = node.fields["retries"]
        ok = not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= MAX_NODE_RETRIES
        if not ok:
            issues.append(
                SpecIssue(
                    "field_value",
                    f"'retries' must be a whole number between 0 and {MAX_NODE_RETRIES}",
                    node_id=node.id,
                    field="retries",
                    example="retries: 1",
                )
            )
    if "max_iterations" in node.fields:
        _check_max_iterations(node.fields["max_iterations"], node.id, "max_iterations", issues)
    # Pipeline STAGES take the same knob and no other validator ever sees the
    # stage dicts — without this, garbage silently runs under the default (or a
    # clamped cap) AND the raw value splits the cell identity of behaviourally
    # identical cells.
    if node.type == "pipeline":
        stages = node.fields.get("stages")
        if isinstance(stages, list):
            for i, stage in enumerate(stages):
                if isinstance(stage, dict) and "max_iterations" in stage:
                    _check_max_iterations(
                        stage["max_iterations"], node.id, f"stages[{i}].max_iterations", issues
                    )


def _check_max_iterations(
    value: object, node_id: str, field: str, issues: list[SpecIssue]
) -> None:
    ok = (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 1 <= value <= MAX_NODE_MAX_ITERATIONS
    )
    if not ok:
        issues.append(
            SpecIssue(
                "field_value",
                f"'max_iterations' must be a whole number between 1 and "
                f"{MAX_NODE_MAX_ITERATIONS}",
                node_id=node_id,
                field=field,
                example="max_iterations: 24",
            )
        )


def _validate_tier(node: Node, issues: list[SpecIssue]) -> None:
    """``tier`` (WF-5) must name one of the CLOSED set of tiers.

    Same footgun as the lifecycle knobs: an unrecognised tier would resolve to
    nothing and the node would quietly run on the default model, which is not
    what "big" was asked for."""
    if "tier" not in node.fields:
        return
    if node.fields["tier"] not in MODEL_TIERS:
        issues.append(
            SpecIssue(
                "field_value",
                f"'tier' must be one of {list(MODEL_TIERS)} (the operator maps "
                "each one to a real model in ~/.lohra/workflow_tiers.json)",
                node_id=node.id,
                field="tier",
                example="tier: big",
            )
        )


def _validate_gate(node: Node, issues: list[SpecIssue]) -> None:
    """A ``gate`` is only a gate if both halves are real (WF-6).

    A body with no prompt or a validator that is not a prompt would leave the
    node spawning nothing and nulling — an author-time error is far cheaper."""
    if node.type != "gate":
        return
    body = node.fields.get("body")
    if not isinstance(body, dict) or not str(body.get("prompt") or "").strip():
        issues.append(
            SpecIssue(
                "field_value",
                "'body' must be an agent-shaped object with a 'prompt' (add "
                "'schema'/'schema_ref' to get validated JSON back)",
                node_id=node.id,
                field="body",
                example='body: {prompt: "Draft the migration plan"}',
            )
        )
    validator = node.fields.get("validator")
    if not isinstance(validator, str) or not validator.strip():
        issues.append(
            SpecIssue(
                "field_value",
                "'validator' must be the prompt a reviewer leaf answers "
                "{ok, feedback} to (the candidate is appended for you)",
                node_id=node.id,
                field="validator",
                example='validator: "Does the plan name every affected file?"',
            )
        )
    if "attempts" in node.fields:
        value = node.fields["attempts"]
        ok = (
            not isinstance(value, bool)
            and isinstance(value, int)
            and 1 <= value <= MAX_GATE_ATTEMPTS
        )
        if not ok:
            issues.append(
                SpecIssue(
                    "field_value",
                    f"'attempts' must be a whole number between 1 and {MAX_GATE_ATTEMPTS}",
                    node_id=node.id,
                    field="attempts",
                    example="attempts: 2",
                )
            )


def _validate_static_fanout(node: Node, issues: list[SpecIssue]) -> None:
    literal = None
    if node.type == "parallel":
        literal = node.fields.get("branches")
    elif node.type == "pipeline":
        literal = node.fields.get("items")
    if isinstance(literal, list) and len(literal) > MAX_STATIC_FANOUT:
        issues.append(
            SpecIssue("fanout_cap", f"static fan-out of {len(literal)} exceeds {MAX_STATIC_FANOUT}; "
                      "use a ${ref} (bounded at runtime by the budget)", node_id=node.id)
        )


def _validate_depends_on(node: Node, node_ids: set[str], issues: list[SpecIssue]) -> None:
    """``depends_on`` (issue #2): a malformed value here does not raise or fail
    loudly downstream — ``graph.dependencies`` and this module's own
    ``_detect_cycles`` both silently DROP anything that is not a known node id
    string (a non-list value, a non-string entry, an unknown id). Before this
    check, that meant a typo'd or mis-shaped ``depends_on`` quietly became NO
    dependency at all — the node could run before the one it was meant to wait
    on, with no error anywhere. Reject it here instead, so the edge a spec asks
    for either exists or the author is told why it can't."""
    if "depends_on" not in node.fields:
        return
    value = node.fields["depends_on"]
    if not isinstance(value, list):
        # An explicit `depends_on: null` (an empty YAML value) is a common
        # slip, not really "a wrong type" — name it plainly rather than
        # printing the Python type of None.
        shape = "empty (omit the field entirely instead)" if value is None else type(value).__name__
        issues.append(
            SpecIssue(
                "depends_on_type",
                f"'depends_on' must be a list of node id strings, not {shape}",
                node_id=node.id, field="depends_on", example='depends_on: ["scan"]',
            )
        )
        return
    for item in value:
        if not isinstance(item, str):
            issues.append(
                SpecIssue(
                    "depends_on_type",
                    f"'depends_on' entries must be node id strings, not {item!r}",
                    node_id=node.id, field="depends_on", example='depends_on: ["scan"]',
                )
            )
        elif item not in node_ids:
            issues.append(
                SpecIssue(
                    "depends_on_target",
                    f"'depends_on' references unknown node id {item!r}",
                    node_id=node.id, field="depends_on", example="reference an existing node id",
                )
            )


def _detect_cycles(nodes: tuple[Node, ...], issues: list[SpecIssue]) -> None:
    ids = {n.id for n in nodes}
    edges: dict[str, set[str]] = {}
    for node in nodes:
        deps = {r for r in _ref_roots(node.fields) if r in ids}
        explicit = node.fields.get("depends_on") or []
        if isinstance(explicit, list):
            deps |= {d for d in explicit if isinstance(d, str) and d in ids}
        edges[node.id] = deps
    state: dict[str, int] = {}  # 0=visiting, 1=done

    def visit(nid: str, stack: tuple[str, ...]) -> bool:
        if state.get(nid) == 1:
            return False
        if state.get(nid) == 0:
            issues.append(
                SpecIssue("cycle", f"dependency cycle: {' -> '.join(stack + (nid,))}", node_id=nid)
            )
            return True
        state[nid] = 0
        for dep in edges.get(nid, ()):
            if visit(dep, stack + (nid,)):
                return True
        state[nid] = 1
        return False

    for node in nodes:
        if state.get(node.id) is None and visit(node.id, ()):
            return


def validate_spec(
    raw: Any, *, supported_types: frozenset[str] | None = None
) -> WorkflowSpec | ValidationError:
    """Validate an authored spec. Returns a WorkflowSpec or a ValidationError.

    ``supported_types`` (when given, e.g. the engine's implemented node types)
    rejects valid-but-not-yet-executable types with a didactic error at author
    time — so a model never authors into a node that would silently null at run."""
    issues: list[SpecIssue] = []
    if not isinstance(raw, dict):
        return ValidationError((SpecIssue("type", "the spec must be a mapping"),))

    meta = _validate_meta(raw.get("meta", {}), issues)
    inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    schemas = _validate_schemas(raw.get("schemas", {}), issues)

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        issues.append(SpecIssue("nodes", "spec needs a non-empty 'nodes' list", field="nodes"))
        return ValidationError(tuple(issues))

    nodes: list[Node] = []
    seen_ids: set[str] = set()
    for index, raw_node in enumerate(nodes_raw):
        node = _validate_node_shape(raw_node, index, issues, supported_types)
        if node is None:
            continue
        if node.id in seen_ids:
            issues.append(SpecIssue("dup_id", f"duplicate node id {node.id!r}", node_id=node.id))
            continue
        seen_ids.add(node.id)
        nodes.append(node)

    node_ids = {n.id for n in nodes}
    for node in nodes:
        _validate_references(node, node_ids, schemas, issues)
        _validate_lifecycle(node, issues)
        _validate_tier(node, issues)
        _validate_gate(node, issues)
        _validate_static_fanout(node, issues)
        _validate_depends_on(node, node_ids, issues)
    _detect_cycles(tuple(nodes), issues)

    if issues:
        return ValidationError(tuple(issues))
    return WorkflowSpec(meta=meta, inputs=inputs, schemas=schemas, nodes=tuple(nodes))
