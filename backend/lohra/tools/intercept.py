"""Compose a tool dispatcher that intercepts stateful tools (spec §6).

Tools like ``memory`` and ``skill_manage`` have their schema in the registry
(so the model sees them) but need per-session state (the MemoryStore, the
SkillStore) to execute. compose_dispatch routes those names to session-bound
handlers and everything else to the registry's stateless dispatch.
"""

from __future__ import annotations

from typing import Any, Callable

BaseDispatch = Callable[[str, dict], str]
InterceptHandler = Callable[[dict], str]


def compose_dispatch(
    base: BaseDispatch, handlers: dict[str, InterceptHandler]
) -> Callable[[str, dict[str, Any]], str]:
    """Return a dispatcher: intercepted names hit their handler, else ``base``."""

    def dispatch(name: str, args: dict[str, Any]) -> str:
        handler = handlers.get(name)
        if handler is not None:
            return handler(args)
        return base(name, args)

    return dispatch
