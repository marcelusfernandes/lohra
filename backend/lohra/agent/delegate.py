"""delegate_task — isolated subagents (spec §6, Phase 5 half B).

The schema lives in the registry so the model sees the tool, but execution is
intercepted (like ``memory``/``skills``) because spawning a child needs the
parent's provider/client. Each task runs in a FRESH ``Agent``:

- no parent history, memory, skills, or context files (isolated context);
- iteration caps — parent 90, child 50;
- ``MAX_DEPTH == 1``: children never receive ``delegate_task`` (no grandchildren);
- at most 3 children run concurrently;
- a child's dangerous shell commands are auto-denied (subagents run unattended).

Children reuse ``run_conversation`` to run their turn, exactly like the parent.

Depth is enforced structurally, not by a counter: a child's definitions omit
``delegate_task`` AND the child's dispatch refuses it by name, so depth can only
ever be 0 (parent) -> 1 (child). ``MAX_DEPTH`` names that invariant.

Limitation: a parent interrupt does not cancel in-flight children — the parent
blocks until the batch finishes (each child is bounded by its 50-iteration cap).
Cooperative child cancellation can land later if needed.
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.agent.agent import Agent, ToolDispatch
from lohra.agent.client_pool import ProviderError, configure_for
from lohra.agent.limits import authored_max_iterations
from lohra.orchestration.core import OrchestrationCore
from lohra.providers.base import ProviderProfile
from lohra.tools.approval import detect_dangerous_command
from lohra.tools.registry import registry, tool_error, tool_result

# How long a delegated child may run before delegate_task gives up waiting.
DELEGATE_TIMEOUT = 300.0

# Iteration caps (spec §6). The parent is bumped to 90 when delegation is wired
# so it has room to dispatch and integrate several rounds of subagent work.
PARENT_MAX_ITERATIONS = 90
CHILD_MAX_ITERATIONS = 50

# Profundidade máxima: 1. The child is depth 1 and gets no delegate_task tool,
# so it can never spawn a grandchild.
MAX_DEPTH = 1

# Tools a subagent must never receive: delegate_task (depth guard) and the
# stateful tools that need a parent-bound store the child does not have.
_CHILD_EXCLUDED_TOOLS = frozenset(
    {
        "delegate_task",
        "memory",
        "skill_view",
        "skill_manage",
        "session_search",
        "cronjob",
        "vision_analyze",
        "image_gen",
        "spawn_session",
        "steer_session",
        "collect_session",
        "run_workflow",
        "workflow_status",
        "workflow_audit",
        "workflow_list",
        "workflow_pause",
        "workflow_cancel",
        "workflow_templates",
        "list_models",
    }
)

SUBAGENT_SYSTEM = (
    "You are an isolated subagent spawned to complete one specific task. You "
    "have no access to the parent conversation, its memory, or its skills, and "
    "you cannot delegate further. Use the available tools to complete the task, "
    "then end with a concise summary of what you did and the outcome."
)

DELEGATE_GUIDANCE = (
    "Delegate one or more self-contained subtasks to fresh, isolated subagents "
    "and wait for their results. Each subagent starts with no knowledge of this "
    "conversation, so every task string must be fully self-contained. Each result "
    "carries a 'sub_id' — to continue that subagent later (it keeps its own "
    "history), call delegate_task again with 'resume_id' set to that sub_id and a "
    "single follow-up instruction in 'tasks'."
)

_SCHEMA = {
    "description": DELEGATE_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Self-contained task descriptions, one per subagent (or a "
                    "single follow-up instruction when resuming)."
                ),
            },
            "resume_id": {
                "type": "string",
                "description": "A sub_id from a prior delegate_task, to continue that subagent.",
            },
            "model": {
                "type": "string",
                "description": "Optional model for the subagents. Omit to inherit the orchestrator's.",
            },
            "provider": {
                "type": "string",
                "description": "Optional provider for the subagents (cross-provider, e.g. 'openai', "
                "'anthropic') — must have credentials configured. Omit to inherit.",
            },
            "effort": {
                "type": "string",
                "description": "Optional reasoning effort for the subagents (where the model supports it).",
            },
            "max_iterations": {
                "type": "integer",
                "description": (
                    "Optional cap on how many provider round-trips each subagent may take "
                    "(1-128). Raise it for long tool-heavy work; omit to inherit the default."
                ),
            },
        },
        "required": ["tasks"],
    },
}

# A factory that builds a fresh child Agent each time it is called.
ChildFactory = Callable[[], Agent]


def child_tool_definitions(parent_definitions: tuple[dict, ...]) -> tuple[dict, ...]:
    """Parent tool definitions minus the tools a subagent must not see."""
    return tuple(
        d
        for d in parent_definitions
        if d.get("function", {}).get("name") not in _CHILD_EXCLUDED_TOOLS
    )


def subagent_dispatch(base: ToolDispatch) -> ToolDispatch:
    """Wrap a base dispatcher with the subagent guards (depth + auto-deny).

    Excluded tools are refused outright (defense in depth — they are already
    absent from the child's definitions), and any dangerous shell command is
    auto-denied because a subagent runs without an operator to approve it.
    """

    def dispatch(name: str, args: dict[str, Any]) -> str:
        if name in _CHILD_EXCLUDED_TOOLS:
            return tool_error(f"the {name!r} tool is not available to subagents")
        if name == "terminal":
            command = args.get("command")
            if isinstance(command, str):
                is_dangerous, _key, desc = detect_dangerous_command(command)
                if is_dangerous:
                    return tool_error(
                        f"subagent auto-denied a dangerous command ({desc})",
                        command=command,
                    )
        return base(name, args)

    return dispatch


def build_child_agent(
    *,
    model: str,
    provider: ProviderProfile,
    client: Any,
    tool_definitions: tuple[dict, ...],
    tool_dispatch: ToolDispatch,
    environment_hints: dict[str, str] | None = None,
) -> Agent:
    """Construct one fresh, isolated child Agent (no parent state)."""
    return Agent(
        model=model,
        provider=provider,
        client=client,
        identity=None,  # no SOUL persona leaks into the child
        system_message=SUBAGENT_SYSTEM,
        context_files=(),  # no parent context files
        environment_hints=environment_hints or {},
        memory_store=None,  # no parent memory
        skill_store=None,  # no parent skills
        tool_definitions=child_tool_definitions(tool_definitions),
        tool_dispatch=tool_dispatch,
        max_iterations=CHILD_MAX_ITERATIONS,
        # context_engine/aux_client intentionally unset: the child gets a fresh,
        # bounded budget (50 iters) and is not compacted.
    )


def make_child_factory(
    *,
    model: str,
    provider: ProviderProfile,
    client: Any,
    tool_definitions: tuple[dict, ...],
    environment_hints: dict[str, str] | None = None,
) -> ChildFactory:
    """Build a ``() -> Agent`` factory that yields a fresh child per call.

    The guarded dispatch is built once and shared (it is stateless); each call
    returns a new Agent so every subagent starts clean. build_child_agent does
    the definition filtering, so the raw parent definitions are passed through.
    """
    dispatch = subagent_dispatch(registry.dispatch)

    def factory() -> Agent:
        return build_child_agent(
            model=model,
            provider=provider,
            client=client,
            tool_definitions=tool_definitions,
            tool_dispatch=dispatch,
            environment_hints=environment_hints,
        )

    return factory


class DelegateTaskTool:
    """Intercepted ``delegate_task`` — runs subagents as RESUMABLE orchestration
    sub-sessions (milestone C).

    Each task spawns an isolated, persistent child via the OrchestrationCore and
    blocks for its result (the batch convenience), returning a ``sub_id`` per
    child so the model can continue it later. ``resume_id`` steers an existing
    child with a follow-up instruction and collects again. The core supplies the
    isolated child factory, concurrency cap, and per-child error isolation, so
    this tool is a thin batch/resume wrapper. Never raises into the dispatcher.
    """

    def __init__(
        self,
        core: OrchestrationCore,
        parent_session_id: str | None = None,
        *,
        timeout: float = DELEGATE_TIMEOUT,
        client_pool: Any | None = None,
    ) -> None:
        self._core = core
        self._parent = parent_session_id
        self._timeout = timeout
        self._pool = client_pool

    def handle(self, args: dict[str, Any]) -> str:
        tasks = args.get("tasks")
        if isinstance(tasks, str):
            tasks = [tasks]
        resume_id = args.get("resume_id")
        if resume_id:
            if args.get("provider"):  # spawn-only: the child's provider is fixed at spawn
                return tool_error("cannot switch provider when resuming a subagent")
            if "max_iterations" in args:  # spawn-only: the cap is fixed at spawn
                return tool_error("cannot change max_iterations when resuming a subagent")
            if not isinstance(tasks, list) or not tasks or not str(tasks[0]).strip():
                return tool_error("resume_id requires a follow-up instruction in 'tasks'")
            steered = self._core.steer(str(resume_id), str(tasks[0]))
            if "error" in steered:
                return tool_error(steered["error"])
            collected = self._core.collect(str(resume_id), wait=True, timeout=self._timeout)
            return tool_result(results=[self._summary(str(resume_id), collected)])

        if not isinstance(tasks, list) or not tasks:
            return tool_error("'tasks' must be a non-empty list of task descriptions")
        if not all(isinstance(t, str) and t.strip() for t in tasks):
            return tool_error("each task must be a non-empty string")

        # Spawn each task in its own try so one bad spawn (a DB/factory error)
        # records an error result instead of aborting the batch and orphaning
        # the children already running. Keeps the "never raises" contract.
        # Optional per-call provider (cross-provider) / model / reasoning effort.
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

        spawned: list[tuple[str | None, str]] = []  # (sub_id, spawn_error)
        for task in tasks:
            try:
                spawned.append((self._core.spawn(task, parent_id=self._parent, configure=configure), ""))
            except Exception as exc:
                spawned.append((None, f"{type(exc).__name__}: {exc}"))
        results = [
            {"sub_id": None, "status": "error", "summary": error}
            if sub_id is None
            else self._summary(sub_id, self._core.collect(sub_id, wait=True, timeout=self._timeout))
            for sub_id, error in spawned
        ]
        return tool_result(results=results)

    @staticmethod
    def _summary(sub_id: str, collected: dict) -> dict:
        if "error" in collected:
            return {"sub_id": sub_id, "status": "error", "summary": collected["error"]}
        return {
            "sub_id": sub_id,
            "status": collected["status"],
            "summary": collected["output"] or "(subagent produced no output)",
        }


def _intercepted_handler(_args: dict[str, Any], **_kwargs: Any) -> str:
    # Reached only if wiring forgot to intercept — fail loudly but safely.
    return tool_error(
        "the delegate_task tool must be intercepted with a session orchestration core"
    )


def register_delegate_task_schema() -> None:
    """Register the schema so the model sees it (execution is intercepted)."""
    registry.register(
        "delegate_task",
        "delegate",
        _SCHEMA,
        _intercepted_handler,
        override=True,
        emoji="👥",
    )
