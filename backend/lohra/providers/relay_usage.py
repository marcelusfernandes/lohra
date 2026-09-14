"""Read Lohra's optional known-floor annotation without inventing a total."""
from dataclasses import fields
from typing import Any

from lohra.agent.types import Usage
from lohra.providers.transports.base import get_field


def relay_annotation(raw: Any) -> Any:
    value = get_field(raw, "lohra_usage")
    return value if value is not None else get_field(get_field(raw, "error"), "lohra_usage")


def relay_floor(raw: Any) -> Usage | None:
    """Only the fixed five nonnegative integer meters constitute an observation."""
    annotation = relay_annotation(raw)
    if get_field(annotation, "status") != "lower_bound":
        return None
    observed = get_field(annotation, "observed")
    values = {field.name: get_field(observed, field.name) for field in fields(Usage)}
    if any(type(value) is not int or value < 0 for value in values.values()):
        return None
    return Usage(**values)


def relay_incomplete(raw: Any) -> bool:
    """An explicit floor/unknown marker never certifies complete usage."""
    return get_field(relay_annotation(raw), "status") in ("lower_bound", "unknown")
