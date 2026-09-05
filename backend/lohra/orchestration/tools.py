"""The agent's orchestration tool triad (intercepted, like ``delegate_task``).

``spawn_session``/``steer_session``/``collect_session`` expose the same
``OrchestrationCore`` the WS surface uses (one core, two consumers). Schemas live
in the registry so the model sees them; execution is bound per session to the
core + the parent's ``session_id`` via the intercept dispatcher. Excluded from
subagents and the server (a sub-session must never spawn more sub-sessions).
"""

from __future__ import annotations

from typing import Any

from lohra.agent.client_pool import ProviderError, configure_for
from lohra.agent.limits import authored_max_iterations
from lohra.orchestration.core import OrchestrationCore
from lohra.tools.registry import registry, tool_error, tool_result

_COLLECT_TIMEOUT = 120.0

SPAWN_GUIDANCE = (
    "Start a parallel sub-session (a fresh, isolated agent) to work on a "
    "self-contained task without blocking you. Returns a 'sub_id' immediately. "
    "The sub-session has no access to this conversation, so the prompt must be "
    "fully self-contained. Use 'steer_session' to add instructions and "
    "'collect_session' to read the result."
)
STEER_GUIDANCE = (
    "Inject an extra instruction into a running sub-session by its 'sub_id'. If "
    "the sub-session is busy the text is queued and read before its next step; "
    "if idle it starts a new turn."
)
COLLECT_GUIDANCE = (
    "Read a sub-session's status and output by its 'sub_id'. Set 'wait' true to "
    "block until its current turn finishes, or false to poll."
)

_SPAWN_SCHEMA = {
    "description": SPAWN_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Self-contained task for the sub-session"},
            "model": {
                "type": "string",
                "description": "Optional model for the sub-session. Omit to inherit the orchestrator's.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider for the sub-session (cross-provider, e.g. 'openai', "
                "'anthropic') — must have credentials configured. Omit to inherit.",
            },
            "effort": {
                "type": "string",
                "description": "Optional reasoning effort (where the model supports it).",
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Optional cap on how many provider round-trips the sub-session may take "
                    "(1-128). Raise it for long tool-heavy work; omit to inherit the default."
                ),
            },
        },
        "required": ["prompt"],
    },
}
_STEER_SCHEMA = {
    "description": STEER_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "sub_id": {"type": "string", "description": "The sub-session id from spawn_session"},
            "text": {"type": "string", "description": "The instruction to inject"},
        },
        "required": ["sub_id", "text"],
    },
}
_COLLECT_SCHEMA = {
    "description": COLLECT_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "sub_id": {"type": "string", "description": "The sub-session id from spawn_session"},
            "wait": {"type": "boolean", "description": "Block until the turn finishes (default false)"},
        },
        "required": ["sub_id"],
    },
}


class OrchestrationTool:
    """Binds the triad to a session's core + parent id."""

    def __init__(
        self,
        core: OrchestrationCore,
        parent_session_id: str | None = None,
        *,
        client_pool: Any | None = None,
    ) -> None:
        self._core = core
        self._parent = parent_session_id
        self._pool = client_pool

    def spawn(self, args: dict[str, Any]) -> str:
        prompt = args.get("prompt")
        if not prompt or not str(prompt).strip():
            return tool_error("spawn_session requires a non-empty 'prompt'")
        model = args.get("model")
        effort = args.get("effort")
        provider = args.get("provider")
        iterations, iter_error = authored_max_iterations(args)
        if iter_error:
            return tool_error(iter_error)
        try:
            configure = configure_for(
                self._pool,
                provider=provider if isinstance(provider, str) and provider else None,
                model=model if isinstance(model, str) and model else None,
                effort=effort if isinstance(effort, str) and effort else None,
                max_iterations=iterations,
            )
        except ProviderError as exc:
            return tool_error(str(exc))
        sub_id = self._core.spawn(str(prompt), parent_id=self._parent, configure=configure)
        return tool_result(sub_id=sub_id)

    def steer(self, args: dict[str, Any]) -> str:
        sub_id, text = args.get("sub_id"), args.get("text")
        if not sub_id or not text or not str(text).strip():
            return tool_error("steer_session requires 'sub_id' and a non-empty 'text'")
        out = self._core.steer(str(sub_id), str(text))
        return tool_error(out["error"]) if "error" in out else tool_result(**out)

    def collect(self, args: dict[str, Any]) -> str:
        sub_id = args.get("sub_id")
        if not sub_id:
            return tool_error("collect_session requires a 'sub_id'")
        out = self._core.collect(
            str(sub_id), wait=bool(args.get("wait")), timeout=_COLLECT_TIMEOUT
        )
        return tool_error(out["error"]) if "error" in out else tool_result(**out)


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("orchestration tools must be intercepted with a session core")


def register_orchestration_tool_schemas() -> None:
    """Register the triad schemas (execution is intercepted)."""
    registry.register(
        "spawn_session", "orchestration", _SPAWN_SCHEMA, _intercepted, override=True, emoji="🌱",
        author_time_only=True,
    )
    registry.register(
        "steer_session", "orchestration", _STEER_SCHEMA, _intercepted, override=True, emoji="🎚️",
        author_time_only=True,
    )
    registry.register(
        "collect_session", "orchestration", _COLLECT_SCHEMA, _intercepted, override=True, emoji="📥",
        author_time_only=True,
    )
