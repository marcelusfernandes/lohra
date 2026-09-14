"""Author-time metadata shared by delegated consumers, never tool JSON."""

from lohra.tools.registry import ToolEntry, tool_error


def author_time_denial(name: str, entry: ToolEntry | None) -> str | None:
    """Judge the registered entry used for advertisement or actual dispatch.

    Consumers bind this check through the registry's existing guard stack;
    ordinary author dispatch has no such restriction.
    """
    if entry is not None and entry.author_time_only:
        return tool_error(f"the {name!r} tool is author-time-only and unavailable to delegated consumers")
    return None
