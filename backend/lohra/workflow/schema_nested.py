"""Validate the FIELDS of embedded, "agent-shaped" payloads — a `parallel`
branch, a `judge_panel` attempt or `synthesize`, a `pipeline` stage, a
`loop_until_dry`/`gate` `body` (issue #82, generalising the class #66 found
first for branches specifically: `schema_ref`/`model` validate on a branch and
the engine never reads them).

``schema.py``'s ``_validate_node_shape`` refuses an unknown/removed field on
the TOP-level node dict against ``NODE_SPECS``. Nothing walked one level down
— ``NESTED_SHAPES`` (nodes.py) is the census of what each embedded payload's
real reader (``strategies.py``/``gates.py``/``prompts.branch_prompt``)
actually consumes; this module refuses anything else, in the same didactic
mould as the ``label``/``phase``/``min_success_ratio`` refusals in schema.py.

``id``/``type: "agent"`` are the ONE deliberate exception (decisão do
coordenador, por delegação do dono, 2026-09-05, after the first cut of this
issue refused them too): they mirror
the top-level node mould but are never read anywhere (a branch/attempt/stage
is not addressable by id — results are positional; the shape is always
agent-like already) — refusing them broke 3 of the owner's own saved
templates and 64+ call sites across this repo's own test suite on upgrade,
for a field that never changed engine behaviour. They are accepted HERE (no
``SpecIssue``) and warned about instead, LOUDLY, by ``lint.py`` — see
``nodes.iter_nested_entries`` (shared by both) and
``lint._lint_nested_id_type``. A ``type`` value OTHER than ``"agent"`` stays
refused below: a branch/attempt/stage can never nest a different node type,
so any other value is misleading, not harmless drift.

A non-dict entry (a bare prompt string — legal everywhere these shapes are
used; ``prompts.branch_prompt`` returns it as-is) has no fields to check and
is skipped here; a wrong-shaped container (an int where a dict belongs, a
non-list where a list belongs) is a RUNTIME fault the strategy itself records
(``strategies.py``'s ``_leaf_prompts``/``run_pipeline``), not this module's.
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.nodes import REMOVED_VISUAL_FIELDS, Node, iter_nested_entries
from lohra.workflow.spec_issues import SpecIssue

# Fields that look like they should route/validate a nested payload but never
# do — named so the refusal explains WHY, not just THAT (issue #66's actual
# complaint: the field validates and the author trusts it). Each shape's OWN
# allow-list still decides what is unknown; this only supplies the extra
# sentence for the fields most likely to fool an author into believing there
# is a guarantee that isn't there.
_ROUTING_KNOBS = frozenset({"model", "tier", "effort", "provider"})


def _hint(key: str, allowed: frozenset[str]) -> str:
    if key in _ROUTING_KNOBS:
        return (
            " Routing lives on the OWNING node only — model/tier/effort/provider "
            "there apply to every leaf it spawns. Written one level down it "
            "used to be silently ignored — no error, no fault, full price, "
            "same session model — which is why it is refused here "
            "(docs/specs/07-workflow-harness.md)."
        )
    if key in ("schema", "schema_ref"):
        if "schema" in allowed:
            return " Put the schema inline under 'schema' — 'schema_ref' has no reader here."
        return " This shape is never schema-validated; put the schema on a downstream node."
    return ""


def _check_entry(
    entry: dict[str, Any],
    *,
    node: Node,
    noun: str,
    allowed: frozenset[str],
    field_path: str,
    issues: list[SpecIssue],
) -> None:
    """``entry`` is always a dict — ``nodes.iter_nested_entries`` (the only
    caller's source) already skips a bare prompt string (legal everywhere
    these shapes are used; it has no fields to check)."""
    for key in entry:
        if key == "id":
            continue  # accepted, warned about by lint.py instead (see module docstring)
        if key == "type":
            if entry.get("type") == "agent":
                continue  # accepted, warned about by lint.py instead
            issues.append(
                SpecIssue(
                    "field_value",
                    f"{node.type} {noun} 'type' must be 'agent' (or omitted) — a "
                    f"{noun} is always agent-shaped, so {entry['type']!r} does not "
                    "nest a different node type here; it just never runs.",
                    node_id=node.id,
                    field=f"{field_path}.type",
                    example="type: agent",
                )
            )
            continue
        if key in REMOVED_VISUAL_FIELDS:
            issues.append(
                SpecIssue(
                    f"{key}_removed",
                    f"'{key}' was removed: the engine never read it here either "
                    "(no live view, progress, event or rollup consumed it, at "
                    f"the top level or nested). Drop it from this {noun}.",
                    node_id=node.id,
                    field=f"{field_path}.{key}",
                    example=f"{{prompt: ...}}  # no '{key}'",
                )
            )
            continue
        if key == "min_success_ratio":
            issues.append(
                SpecIssue(
                    "min_success_ratio_removed",
                    "'min_success_ratio' was removed: the engine never enforced "
                    "it here either. Check the fan-out's own result in a "
                    "downstream 'gate'/'completeness_check' node instead.",
                    node_id=node.id,
                    field=f"{field_path}.{key}",
                )
            )
            continue
        if key not in allowed:
            issues.append(
                SpecIssue(
                    "nested_unknown_field",
                    f"{node.type} {noun} has no field {key!r} — a {noun} here "
                    f"only reads {sorted(allowed)}." + _hint(key, allowed),
                    node_id=node.id,
                    field=f"{field_path}.{key}",
                    example=f"allowed: {sorted(allowed)}",
                )
            )
    if (
        "schema" in entry
        and "schema_ref" in entry
        and "schema" in allowed
        and "schema_ref" in allowed
    ):
        issues.append(
            SpecIssue(
                "schema_xor",
                f"use either 'schema' or 'schema_ref' on this {noun}, not both",
                node_id=node.id,
                field=f"{field_path}.schema_ref",
            )
        )


def validate_nested_shapes(node: Node, issues: list[SpecIssue]) -> None:
    """Walk every embedded shape ``node.type`` declares (``nodes.NESTED_SHAPES``,
    via ``nodes.iter_nested_entries``) and refuse an unknown/removed field
    inside it, same as the top level — except ``id``/``type: "agent"`` (see
    module docstring)."""
    for field_path, shape, entry in iter_nested_entries(node):
        _check_entry(
            entry,
            node=node,
            noun=shape.noun,
            allowed=shape.fields,
            field_path=field_path,
            issues=issues,
        )
