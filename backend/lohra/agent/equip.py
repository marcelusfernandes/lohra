"""Equip an agent with the full local toolset, including self-improving tools.

Registers the built-in (fs, terminal) and intercepted (memory, skills) tool
schemas, and builds a per-session dispatcher that routes the stateful tools to
that session's MemoryStore/SkillStore while everything else hits the registry.
Shared by `lohra chat` and the dashboard gateway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lohra.agent.agent import ToolDispatch
from lohra.agent.delegate import (
    DelegateTaskTool,
    register_delegate_task_schema,
)
from lohra.agent.taint import TaintTracker, taint_wrap
from lohra.catalog.tool import ListModelsTool, register_list_models_tool_schema
from lohra.cron.store import CronStore
from lohra.cron.tool import CronTool, register_cron_tool_schema
from lohra.imagegen.tool import ImageGenRunner, ImageGenTool, register_image_gen_tool_schema
from lohra.memory.store import MemoryStore
from lohra.orchestration.core import OrchestrationCore
from lohra.orchestration.tools import OrchestrationTool, register_orchestration_tool_schemas
from lohra.memory.tool import MemoryTool, register_memory_tool_schema
from lohra.skills.store import SkillStore, builtin_root
from lohra.skills.tool import SkillTool, register_skill_tool_schemas
from lohra.state.db import SessionDB
from lohra.state.search import SessionSearchTool, register_session_search_schema
from lohra.tools import load_builtin_tools, registry
from lohra.tools.intercept import compose_dispatch
from lohra.vision.tool import VisionRunner, VisionTool, register_vision_tool_schema
from lohra.workflow.audit_query import (
    WorkflowAuditTool,
    register_workflow_audit_schema,
)
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool, register_workflow_tool_schemas


def register_all_tools() -> None:
    """Register every tool schema the model should see (idempotent)."""
    load_builtin_tools()
    register_memory_tool_schema()
    register_skill_tool_schemas()
    register_session_search_schema()
    register_delegate_task_schema()
    register_cron_tool_schema()
    register_vision_tool_schema()
    register_image_gen_tool_schema()
    register_orchestration_tool_schemas()
    register_workflow_tool_schemas()
    register_workflow_audit_schema()
    register_list_models_tool_schema()


def bind_workflow_notifier(service: WorkflowService, resolve_inbox: Any) -> None:
    """Announce a finished workflow run in the steer inbox of the session that
    launched it (M6).

    The inbox is the ONLY legal channel: the system prompt is built once per
    session and frozen (Invariante #1), so a run that finishes mid-turn reaches
    the agent as a system-reminder in the tail of the next loop iteration — never
    as a rewrite of the prompt the provider is caching.

    ``resolve_inbox(session_id)`` returns anything with ``enqueue_steer`` (a
    GatewaySession), or None. A run nobody owns — and a session that has gone
    away — is a silent no-op, never a crash: the notification is a courtesy, and
    the rollup is always there to poll.
    """

    def on_run_done(run_id: str, _status: str, summary: str) -> None:
        session_id = service.run_owner(run_id)
        inbox = resolve_inbox(session_id) if session_id else None
        if inbox is not None:
            inbox.enqueue_steer(summary)

    service.set_on_run_done(on_run_done)


def build_session_stores(
    home: Path, skill_roots: tuple[Path, ...] = ()
) -> tuple[MemoryStore, SkillStore]:
    """Session stores. ``skill_roots`` are project skill dirs scanned (with
    precedence) alongside the home skills, so Lohra sees the project's skills.

    The builtin tier (skills shipped with Lohra) is scanned LAST, so a home or
    project copy of the same name always overrides it."""
    return MemoryStore(home), SkillStore(
        home, extra_roots=skill_roots, builtin_roots=(builtin_root(),)
    )


def build_session_dispatch(
    memory_store: MemoryStore,
    skill_store: SkillStore,
    db: SessionDB | None = None,
    cron_store: CronStore | None = None,
    vision_runner: VisionRunner | None = None,
    image_gen_runner: ImageGenRunner | None = None,
    orchestration_core: OrchestrationCore | None = None,
    session_id: str | None = None,
    workflow_service: WorkflowService | None = None,
    client_pool: Any | None = None,
    home: Path | None = None,
) -> ToolDispatch:
    """Dispatcher binding intercepted tools to this session's stores/db.

    When ``orchestration_core`` is given, both the orchestration triad and the
    (now resumable) ``delegate_task`` are bound to it — delegate_task runs its
    subagents as core sub-sessions. Otherwise those tools fall to the registry's
    fail-safe intercepted handlers.

    ``home`` binds the read-only ``list_models`` catalog to this workspace — its
    tier map and subscription opt-in both live under that root.
    """
    handlers = {
        "memory": MemoryTool(memory_store).handle,
        "skill_view": SkillTool(skill_store).view,
        "skill_manage": SkillTool(skill_store).manage,
    }
    if home is not None:
        handlers["list_models"] = ListModelsTool(home).handle
    if db is not None:
        handlers["session_search"] = SessionSearchTool(db).handle
        handlers["workflow_audit"] = WorkflowAuditTool(db).handle
    if cron_store is not None:
        handlers["cronjob"] = CronTool(cron_store).handle
    if vision_runner is not None:
        handlers["vision_analyze"] = VisionTool(vision_runner).handle
    if image_gen_runner is not None:
        handlers["image_gen"] = ImageGenTool(image_gen_runner).handle
    if orchestration_core is not None:
        triad = OrchestrationTool(orchestration_core, session_id, client_pool=client_pool)
        handlers["spawn_session"] = triad.spawn
        handlers["steer_session"] = triad.steer
        handlers["collect_session"] = triad.collect
        handlers["delegate_task"] = DelegateTaskTool(
            orchestration_core, session_id, client_pool=client_pool
        ).handle
    dispatch = compose_dispatch(registry.dispatch, handlers)
    if workflow_service is not None:
        # Taint (spec §8.2): if this session's turn ingests web/MCP content, the
        # tracker is marked; run_workflow then runs leaves with reduced capability.
        tracker = TaintTracker()
        workflow = WorkflowTool(workflow_service, taint=tracker, owner=session_id)
        handlers["run_workflow"] = workflow.run
        handlers["workflow_status"] = workflow.status
        handlers["workflow_list"] = workflow.list
        handlers["workflow_pause"] = workflow.pause
        handlers["workflow_cancel"] = workflow.cancel
        handlers["workflow_templates"] = workflow.templates
        # Rebuild the dispatch with the workflow handlers, then wrap so a tainting
        # tool anywhere in the turn marks the tracker run_workflow reads.
        dispatch = taint_wrap(compose_dispatch(registry.dispatch, handlers), tracker)
    return dispatch
