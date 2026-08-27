"""Per-spawn Agent overrides via the core.spawn ``configure`` hook.

A child/leaf/sub-session inherits the orchestrator's fixed model by default. These
build a ``configure(agent)`` hook that overrides per-spawn knobs — model (same
provider only), reasoning effort, the iteration leash, and the forced
structured-output tool — leaving unset fields unchanged. Returns None when
nothing is overridden (byte-identical).
"""

from __future__ import annotations

from typing import Any, Callable


def make_configure(
    *,
    model: str | None = None,
    effort: str | None = None,
    forced_tool: dict | None = None,
    provider: Any | None = None,
    client: Any | None = None,
    max_iterations: int | None = None,
) -> Callable[[Any], None] | None:
    """A configure hook applying the set overrides; None if there's nothing to do.

    ``provider`` + ``client`` are an ATOMIC pair (cross-provider delegation): set
    BOTH or neither — provider drives the transport, client is the matching SDK, so
    one without the other is a silent 400. Pure: assignment only, no I/O (the caller
    resolves provider/client). The transport follows from agent.provider for free."""
    if (provider is None) != (client is None):
        raise ValueError("provider and client must be set together (atomic swap)")
    if (
        model is None
        and effort is None
        and forced_tool is None
        and provider is None
        and max_iterations is None
    ):
        return None

    def configure(agent: Any) -> None:
        if provider is not None:  # swap the trio together
            agent.provider = provider
            agent.client = client
        if model is not None:
            agent.model = model
        if effort is not None:
            agent.effort = effort
        if forced_tool is not None:
            agent.forced_tool = forced_tool
        if max_iterations is not None:
            agent.max_iterations = max_iterations

    return configure
