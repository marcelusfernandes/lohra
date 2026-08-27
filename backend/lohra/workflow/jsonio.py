"""Tolerant JSON extraction (no deps beyond stdlib).

Models routinely wrap JSON in ```json fences``` or surrounding prose. Strict
json.loads chokes on that, which would null an otherwise-valid leaf or break a
downstream ${node.field} lookup. Shared by validation (schema check) and refs
(path descent into a JSON-string output).
"""

from __future__ import annotations

import json
import re
from typing import Any

UNPARSEABLE = object()
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _first_balanced(text: str, opener: str, closer: str) -> str | None:
    """The first balanced opener..closer span (best-effort; ignores quoting)."""
    start = text.find(opener)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _candidates(text: str):
    yield text  # the common case: clean JSON
    for match in _FENCE_RE.finditer(text):  # ```json ... ``` blocks
        yield match.group(1)
    for opener, closer in (("{", "}"), ("[", "]")):  # JSON embedded in prose
        span = _first_balanced(text, opener, closer)
        if span:
            yield span


def loads_lenient(text: str) -> Any:
    """Parse JSON tolerantly (fences/prose). Returns UNPARSEABLE if nothing parses."""
    for candidate in _candidates(text):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return UNPARSEABLE
