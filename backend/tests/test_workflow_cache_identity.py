"""The two pure pieces of a cell's identity that MORE THAN ONE reader needs.

``spec_identity`` namespaces every cell of a run; ``resolve_schema`` turns a
node's authored ``schema``/``schema_ref`` into the dict that goes INTO the hash.
Both used to live inside the engine, where only the engine could reach them —
and anything recomputing a key later (cache_preview) had to re-derive the
defaults by hand. A drifted default there silently re-keys every row, so these
tests pin the shared definitions and pin that the engine reads THEM.
"""

from lohra.workflow.cache import spec_identity
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.nodes import resolve_schema
from lohra.workflow.schema import validate_spec


def _spec(meta=None, schemas=None):
    return validate_spec(
        {
            "meta": meta if meta is not None else {"name": "demo", "version": 1},
            **({"schema": schemas} if schemas else {}),
            "nodes": [{"id": "a", "type": "agent", "prompt": "hi"}],
        }
    )


# --- spec_identity ---


def test_spec_identity_reads_name_and_version():
    assert spec_identity(_spec({"name": "demo", "version": "4.0"})) == ("demo", "4.0")


def test_spec_identity_defaults_are_empty_name_and_zero_version():
    # The engine's own defaults, byte-for-byte: a cell written by a spec with no
    # meta must still be findable by a recomputation.
    assert spec_identity(_spec({})) == ("", 0)


def test_engine_namespaces_cells_with_spec_identity():
    from lohra.workflow.cache import content_hash

    engine = WorkflowEngine(core=None, budget=None)
    engine._spec_id = spec_identity(_spec({"name": "demo", "version": 7}))  # what run() sets
    assert engine.cell_hash("a", "agent") == content_hash("demo", 7, "a", "agent")


# --- resolve_schema ---


def test_resolve_schema_inline_dict_wins():
    assert resolve_schema({}, {"schema": {"type": "object"}}) == {"type": "object"}


def test_resolve_schema_coerces_the_name_mixup():
    # `schema: "name"` is the common authoring mix-up — resolved, never dropped.
    assert resolve_schema({"S": {"type": "string"}}, {"schema": "S"}) == {"type": "string"}


def test_resolve_schema_ref_and_missing():
    assert resolve_schema({"S": {"type": "string"}}, {"schema_ref": "S"}) == {"type": "string"}
    assert resolve_schema({}, {"schema_ref": "nope"}) is None
    assert resolve_schema({}, {}) is None


def test_engine_resolve_schema_delegates_to_the_shared_function():
    engine = WorkflowEngine(core=None, budget=None)
    engine._schemas = {"S": {"type": "string"}}
    for fields in ({"schema": {"type": "object"}}, {"schema": "S"}, {"schema_ref": "S"}, {}):
        assert engine.resolve_schema(fields) == resolve_schema(engine._schemas, fields)
