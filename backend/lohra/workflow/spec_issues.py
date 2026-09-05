"""The validator's issue/error types — extracted from ``schema.py`` so
``schema_nested.py`` can build the same ``SpecIssue`` objects without a circular
import (``schema.py`` calls into ``schema_nested.py``; both need this).

Re-exported by ``schema.py`` (``from lohra.workflow.spec_issues import
SpecIssue, ValidationError``), so every existing ``from lohra.workflow.schema
import SpecIssue`` / ``ValidationError`` call site keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
