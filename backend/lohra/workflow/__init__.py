"""Dynamic workflow harness (Fase 8) — declarative typed DAG, no code execution.

The Lohra agent emits an INERT typed spec (a dict, usually authored as YAML/JSON);
a WorkflowEngine walks it over a CLOSED set of node types. There is no eval, no
DSL runtime, no model-generated code path — engine-escape is eliminated by
construction. See docs/specs/07-workflow-harness.md.

Milestone A (this slice): the spec model (`nodes`), the validator (`schema`,
returns a ValidationError, never raises), and single-pass reference resolution
(`refs`, the second-order-injection guard). No execution yet.
"""
