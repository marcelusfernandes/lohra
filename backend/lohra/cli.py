"""Lohra command-line entry point.

Subcommands (incremental — see docs/ROADMAP.md):
  lohra --version
  lohra chat "<prompt>"      (Phase 1)
  lohra dashboard            (Phase 3 — FastAPI gateway on port 9119)
"""

from __future__ import annotations

import argparse
import os
import sys

from lohra import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lohra", description="Lohra AI agent")
    parser.add_argument("--version", action="version", version=f"lohra {__version__}")

    # A shared parent so `--profile` works positioned after the subcommand
    # (e.g. `lohra chat "hi" --profile work`). It re-roots all state under
    # ~/.lohra/profiles/<name>/ — see lohra.memory.paths.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profile",
        help="isolated workspace name (own memory/skills/sessions/cron/mcp); default: shared home",
    )
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser(
        "chat",
        help="send a prompt to the agent",
        parents=[common],
        epilog="LOHRA_LIVEVIEW=fancy|plain|off picks the workflow live view "
        "(default: fancy = a block redrawn in place on a terminal, plain = append lines elsewhere)",
    )
    chat.add_argument("prompt", help="the prompt text")
    chat.add_argument("--model", help="model id (defaults to the provider's first fallback)")
    chat.add_argument("--provider", help="provider name (defaults to arg→config→env→auto)")
    chat.add_argument("--session", help="session id to create or resume (default: new)")
    chat.add_argument("--no-tools", action="store_true", help="disable tools (chat only)")
    chat.add_argument("--yolo", action="store_true", help="auto-approve dangerous commands")
    chat.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit a structured JSON envelope (input/output/reasoning/tool_calls/model/usage) "
        "instead of plain text — for orchestration",
    )
    chat.add_argument(
        "--max-parallel",
        type=int,
        help="max orchestration sub-sessions running at once "
        "(default 4; also LOHRA_MAX_PARALLEL env var)",
    )

    dash = sub.add_parser("dashboard", help="run the gateway server (Phase 3)", parents=[common])
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=9119)
    dash.add_argument("--no-open", action="store_true")
    dash.add_argument("--insecure", action="store_true")

    serve = sub.add_parser(
        "serve", help="run the OpenAI-compatible server (Phase 6)", parents=[common]
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--insecure", action="store_true", help="disable the API key")
    serve.add_argument(
        "--tools",
        default="",
        help="comma-separated tool allow-list to run server-side (agentic mode; "
        "DANGEROUS — e.g. 'terminal' is remote code execution). Default: relay, no tools.",
    )

    cron = sub.add_parser("cron", help="manage scheduled jobs (Phase 6)", parents=[common])
    cron.add_argument("action", choices=["list", "add", "remove", "pause", "resume"])
    cron.add_argument("job_id", nargs="?", help="target job id (remove/pause/resume)")
    cron.add_argument("--name", help="job name (add)")
    cron.add_argument("--prompt", help="the instruction each run executes (add)")
    cron.add_argument("--interval", type=int, help="minutes between runs (add)")
    cron.add_argument("--cron", dest="cron_expr", help="5-field cron expression (add)")
    cron.add_argument("--at", type=float, help="run-at epoch timestamp (add, once)")

    wf = sub.add_parser(
        "workflow", help="look at workflow runs (reads the durable state; no LLM)",
        parents=[common],
    )
    wf_sub = wf.add_subparsers(dest="workflow_cmd", required=True)
    wf_list = wf_sub.add_parser("list", help="recent runs: status, nodes done, tokens")
    wf_list.add_argument("--limit", type=int, default=20)
    wf_watch = wf_sub.add_parser("watch", help="follow a run until it stops")
    wf_watch.add_argument("run_id", nargs="?", help="the run to follow (or --last)")
    wf_watch.add_argument("--last", action="store_true", help="follow the most recent run")
    wf_watch.add_argument("--poll", type=float, default=2.0, help="seconds between polls")

    prof = sub.add_parser("profile", help="manage isolated workspaces (Phase 6)")
    prof.add_argument("action", choices=["list", "create"])
    prof.add_argument("name", nargs="?", help="profile name (create)")

    auth = sub.add_parser(
        "auth", help="OpenAI/Codex subscription mode (Phase 10, opt-in, ToS-gray)", parents=[common]
    )
    auth.add_argument("action", choices=["status", "enable", "disable", "login", "logout"])
    auth.add_argument("--yes", action="store_true", help="skip the ToS confirmation prompt (enable)")

    sk = sub.add_parser("skill", help="skill kits for other agents (export)")
    sk_sub = sk.add_subparsers(dest="skill_cmd", required=True)
    sk_exp = sk_sub.add_parser("export", help="print or write a packaged skill kit (e.g. use-lohra)")
    sk_exp.add_argument("name", help="kit name (see error message for the list)")
    sk_exp.add_argument("--to", help="write <DIR>/<name>/SKILL.md instead of printing to stdout")

    upd = sub.add_parser("update", help="pull the latest backend code (Phase 6)")
    upd.add_argument(
        "--check", action="store_true", help="check for updates without applying them"
    )
    upd.add_argument(
        "--reinstall", action="store_true", help="reinstall the package after updating (deps)"
    )

    return parser


# api_modes with both a transport and a client wired (see agent.client.build_client).
_SUPPORTED_API_MODES = ("anthropic_messages", "chat_completions")


def _resolve_profile(provider: str | None):
    """Resolve to a usable provider profile, or (None, exit_code) on failure."""
    from lohra.providers import get_provider_profile, resolve_provider_name

    try:
        name = resolve_provider_name(arg=provider)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    if name == "auto":
        print(
            "no provider configured — set an API key (ANTHROPIC_API_KEY, "
            "OPENAI_API_KEY, ...) or pass --provider.",
            file=sys.stderr,
        )
        return None, 2
    profile = get_provider_profile(name)
    if profile is None:
        print(f"unknown provider {name!r}.", file=sys.stderr)
        return None, 2
    if profile.api_mode not in _SUPPORTED_API_MODES:
        print(
            f"provider {name!r} (api_mode {profile.api_mode!r}) is not supported yet.",
            file=sys.stderr,
        )
        return None, 2
    return profile, 0


def _resolve_model(profile, model: str | None) -> str | None:
    """The chosen model: explicit override, else the profile's first fallback."""
    if model:
        return model
    return profile.fallback_models[0] if profile.fallback_models else None


def _cli_approval(command: str, description: str, *, allow_permanent: bool = False) -> str:
    """Prompt the operator on stderr to approve a dangerous command."""
    sys.stderr.write(f"\n⚠️  Dangerous command — {description}:\n  {command}\n")
    sys.stderr.write("Approve? [o]nce / [s]ession / [d]eny: ")
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        return "deny"  # non-interactive stdin -> fail safe
    return {"o": "once", "s": "session", "d": "deny"}.get(answer[:1], "deny")


def run_chat(
    prompt: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    session: str | None = None,
    use_tools: bool = True,
    yolo: bool = False,
    max_parallel: int | None = None,
    json_output: bool = False,
) -> int:
    """Chat against the resolved provider, with tools and session persistence."""
    import os
    from pathlib import Path
    from uuid import uuid4

    from lohra.agent import Agent, run_conversation
    from lohra.agent.aux import AuxClient
    from lohra.agent.client import build_client
    from lohra.agent.client_pool import ClientPool
    from lohra.agent.context import ContextCompressor
    from lohra.agent.delegate import PARENT_MAX_ITERATIONS, make_child_factory
    from lohra.agent.equip import build_session_dispatch, build_session_stores, register_all_tools
    from lohra.cron.store import CronStore
    from lohra.mcp import register_configured_mcp_servers
    from lohra.memory.paths import lohra_home, state_db_path
    from lohra.memory.soul import load_soul
    from lohra.providers.transports import get_transport
    from lohra.state import SessionDB
    from lohra.imagegen.tool import make_image_gen_runner
    from lohra.orchestration.core import OrchestrationCore, resolve_limits
    from lohra.project.discover import discover_skill_roots, load_project_context
    from lohra.workflow.service import WorkflowService
    from lohra.state.compression_lock import compression_lock
    from lohra.tools import approval, registry
    from lohra.vision.tool import make_vision_runner

    # Subscription mode (Fase 10, opt-in) is resolved BEFORE provider/key resolution:
    # its whole point is the no-API-key path, so it must not fall into the "set an
    # API key" branch. OpenAI only; when off, the default API-key path is unchanged.
    from lohra.subscription.credentials import SubscriptionError, subscription_active
    from lohra.subscription.provider import CODEX_PROVIDER, build_subscription_client, codex_default_model

    def _json_err(message: str) -> None:
        # --json contract: stdout is ALWAYS exactly one parseable object, even on a
        # pre-turn failure. The human message still goes to stderr at the call site.
        if json_output:
            import json as _json

            from lohra.agent.result_json import error_envelope

            print(_json.dumps(error_envelope(prompt, message, model=model), ensure_ascii=True))

    sub_client = None
    if subscription_active(lohra_home()):
        if provider:  # subscription overrides; don't silently discard an explicit choice
            print(f"subscription mode active — ignoring --provider {provider}.", file=sys.stderr)
        profile = CODEX_PROVIDER
        if not model:  # default to the user's Codex-configured model (slugs vary)
            model = codex_default_model()
        try:
            sub_client = build_subscription_client(lohra_home())
        except SubscriptionError as exc:
            print(f"subscription mode: {exc}", file=sys.stderr)
            _json_err(f"subscription mode: {exc}")
            return 2
        print("⚠️  using your OpenAI/Codex subscription (opt-in, ToS-gray).", file=sys.stderr)
    else:
        profile, code = _resolve_profile(provider)
        if profile is None:
            _json_err("provider not configured (see stderr)")
            return code

    chosen_model = _resolve_model(profile, model)
    if chosen_model is None:
        msg = f"provider {profile.name!r} has no default model — pass --model."
        print(msg, file=sys.stderr)
        _json_err(msg)
        return 2

    try:
        client = sub_client if sub_client is not None else build_client(profile)
    except Exception as exc:  # missing SDK, or an auth/config error at construction
        msg = f"could not initialize the {profile.name} client: {exc}"
        print(msg, file=sys.stderr)
        _json_err(msg)
        return 2

    db = SessionDB(str(state_db_path()))
    session_id = session or uuid4().hex  # needed before the dispatch (parent id)

    tool_definitions: tuple[dict, ...] = ()
    tool_dispatch = None
    memory_store = skill_store = None
    mcp_manager = None
    orchestration_core = None
    workflow_service = None
    client_pool = None
    if use_tools:
        register_all_tools()
        # Connect any configured MCP servers BEFORE snapshotting definitions, so
        # their tools are visible to the model this session (best-effort).
        mcp_manager = register_configured_mcp_servers(registry)
        tool_definitions = tuple(registry.get_definitions())
        memory_store, skill_store = build_session_stores(
            lohra_home(), discover_skill_roots(Path(os.getcwd()))
        )
        child_factory = make_child_factory(
            model=chosen_model,
            provider=profile,
            client=client,
            tool_definitions=tool_definitions,
        )
        vision_runner = make_vision_runner(
            client, get_transport(profile.api_mode), chosen_model
        )
        image_gen_runner = make_image_gen_runner(client, str(lohra_home() / "images"))
        # Orchestration sub-sessions reuse the isolated subagent child factory.
        # Limits: --max-parallel flag > LOHRA_MAX_PARALLEL/LOHRA_MAX_SUBSESSIONS env > defaults.
        max_concurrent, max_children = resolve_limits(max_parallel=max_parallel)
        orchestration_core = OrchestrationCore(
            db, child_factory, max_concurrent=max_concurrent, max_children=max_children
        )
        # Cross-provider delegation: a pool that builds a client per target provider
        # (borrows the parent client; closed in the finally). None override = inherit.
        client_pool = ClientPool(parent_provider=profile, parent_client=client, home=lohra_home())
        # Workflow harness: its sandboxed leaves reuse the same isolated child factory.
        # on_event = the LIVE VIEW (WF-30): the plan at launch, then every node
        # transition, fan-out count and fault as it happens. Unconditionally on
        # STDERR — stdout carries the streamed answer, or (under --json) exactly
        # one envelope, and a progress line must never land in either.
        workflow_service = WorkflowService(
            base_child_factory=child_factory,
            db=db,
            home=lohra_home(),
            client_pool=client_pool,
            on_event=_live_workflow_view(sys.stderr),
        )
        tool_dispatch = build_session_dispatch(
            memory_store,
            skill_store,
            db,
            CronStore(lohra_home()),
            vision_runner,
            image_gen_runner,
            orchestration_core,
            session_id,
            workflow_service,
            client_pool=client_pool,
        )
        approval.set_yolo(yolo)
        approval.set_callback(None if yolo else _cli_approval)

    aux_client = AuxClient(
        client=client,
        transport=get_transport(profile.api_mode),
        model=profile.default_aux_model or chosen_model,
    )
    # Project context (Fase 9): read AGENTS.md/CLAUDE.md from the cwd's project so
    # the frozen prompt carries them and Lohra follows the project's instructions.
    project_context, env_hints = load_project_context(Path(os.getcwd()))
    agent = Agent(
        model=chosen_model,
        provider=profile,
        client=client,
        identity=load_soul(lohra_home()),
        context_files=project_context,
        environment_hints=env_hints,
        tool_definitions=tool_definitions,
        tool_dispatch=tool_dispatch,
        memory_store=memory_store,
        skill_store=skill_store,
        # Room to dispatch subagents and integrate several rounds of their work.
        # Harmless without tools: a chat-only turn ends after its first response.
        max_iterations=PARENT_MAX_ITERATIONS,
        context_engine=ContextCompressor(),
        aux_client=aux_client,
    )
    if db.get_session(session_id) is None:
        db.create_session(
            session_id,
            model=agent.model,
            system_prompt=agent.system_prompt().text,
            cwd=os.getcwd(),
        )
    prior = db.load_messages(session_id)

    streamed = False

    def on_text(text: str) -> None:
        nonlocal streamed
        streamed = True
        sys.stdout.write(text)
        sys.stdout.flush()

    try:
        try:
            # JSON mode: no live streaming, so stdout carries ONLY the envelope.
            result = run_conversation(
                agent,
                prompt,
                conversation_history=prior,
                stream_delta_callback=None if json_output else on_text,
            )
        except Exception as exc:  # never let an internal error dump a traceback
            print(f"\nerror: {exc}", file=sys.stderr)
            _json_err(f"{exc}")
            return 1
        # Persist only a cleanly-completed turn. An errored or interrupted turn
        # would leave a dangling user/tool message that breaks API alternation
        # when the session is resumed — drop it; the user can retry.
        if not result["error"] and not result["interrupted"]:
            if result["compacted"]:
                # Compaction rewrote the history — fork a child session (lineage
                # split) and persist the full compressed transcript there. The
                # cross-process lock keeps a concurrent run on the same session
                # from forking a divergent child.
                with compression_lock(db, session_id) as acquired:
                    if acquired:
                        db.end_session(session_id, "compression")
                        child_id = uuid4().hex
                        db.create_session(
                            child_id, parent_session_id=session_id, model=agent.model, cwd=os.getcwd()
                        )
                        for message in result["messages"]:
                            db.save_message(child_id, message)
                        session_id = child_id
                    else:
                        print(
                            "note: another process is compacting this session; "
                            "this turn was not saved.",
                            file=sys.stderr,
                        )
            else:
                for message in result["messages"][len(prior):]:
                    db.save_message(session_id, message)
    finally:
        if workflow_service is not None:
            workflow_service.shutdown()
        if orchestration_core is not None:
            orchestration_core.shutdown()  # interrupt sub-sessions, join the pool
        if client_pool is not None:
            client_pool.close()  # close only the cross-provider clients it built
        if mcp_manager is not None:
            mcp_manager.shutdown()
        client.close()
        db.close()

    if json_output:
        import json as _json

        from lohra.agent.result_json import build_envelope

        envelope = build_envelope(
            prompt, result,
            model=chosen_model, temperature=agent.temperature, session_id=session_id,
        )
        # ensure_ascii=True: encoding-independent stdout (a lone surrogate in
        # provider content would otherwise raise UnicodeEncodeError → empty stdout).
        print(_json.dumps(envelope, ensure_ascii=True, indent=2))
    elif streamed:
        sys.stdout.write("\n")
    elif not result["error"]:
        print(result["final_response"] or "")
    print(f"session: {session_id}  (resume with --session {session_id})", file=sys.stderr)

    if result["error"]:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1
    return 0


def _isatty(stream) -> bool:
    """Whether ``stream`` is a real terminal. Some wrappers RAISE on ``isatty``,
    and guessing "terminal" there would paint escape codes into a log file."""
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _live_workflow_view(stream):
    """A workflow event sink that renders to ``stream`` (always stderr), or None.

    Two modes, one fallback. On a real terminal the run is a BLOCK redrawn in
    place (WF-31) that freezes when it ends, so the agent's answer lands under
    it. Off a terminal — a pipe, a log, a dumb TERM — it is the append lines
    that read the same everywhere. ``LOHRA_LIVEVIEW`` forces either, or ``off``.

    Kept tiny and total: rendering is somebody else's module, and a run must
    never be able to die because a terminal could not take a line."""
    from lohra.workflow.liveview import render_event, write_lines
    from lohra.workflow.liveview_tui import FANCY, OFF, LivePainter, select_mode

    mode = select_mode(
        isatty=_isatty(stream),
        term=os.environ.get("TERM"),
        env=os.environ.get("LOHRA_LIVEVIEW"),
    )
    if mode == OFF:
        return None
    if mode == FANCY:
        return LivePainter(stream)

    def on_event(run_id: str, kind: str, payload: dict) -> None:
        write_lines(render_event(run_id, kind, payload), stream)

    return on_event


def build_dashboard_app(*, insecure: bool):
    """Build (manager, app, token) for the dashboard, or (None, None, code) on failure.

    Separated from serving so the wiring is testable without running uvicorn.
    """
    import os
    import secrets
    import threading

    from lohra.agent import Agent
    from lohra.agent.client import build_client
    from lohra.agent.client_pool import ClientPool
    from lohra.agent.delegate import PARENT_MAX_ITERATIONS, make_child_factory
    from lohra.agent.equip import (
        bind_workflow_notifier,
        build_session_dispatch,
        build_session_stores,
        register_all_tools,
    )
    from lohra.cron.runner import make_cron_runner
    from lohra.cron.scheduler import run_scheduler_loop
    from lohra.cron.store import CronStore
    from lohra.gateway.app import create_app
    from lohra.gateway.manager import SessionManager
    from lohra.imagegen.tool import make_image_gen_runner
    from lohra.mcp import register_configured_mcp_servers
    from lohra.memory.paths import lohra_home, state_db_path
    from lohra.memory.soul import load_soul
    from pathlib import Path

    from lohra.orchestration.core import OrchestrationCore, resolve_limits
    from lohra.project.discover import discover_skill_roots, load_project_context
    from lohra.providers.transports import get_transport
    from lohra.state import SessionDB
    from lohra.workflow.service import WorkflowService
    from lohra.tools import registry
    from lohra.vision.tool import make_vision_runner

    # Subscription mode (Fase 10): resolve BEFORE provider/key, like run_chat, so a
    # subscription-only desktop user (no API key) isn't dropped into the key path.
    from lohra.subscription.credentials import SubscriptionError, subscription_active
    from lohra.subscription.provider import (
        CODEX_PROVIDER,
        build_subscription_client,
        codex_default_model,
    )

    shared_client = None
    if subscription_active(lohra_home()):
        profile = CODEX_PROVIDER
        model_override = os.environ.get("LOHRA_MODEL") or codex_default_model()
        try:
            shared_client = build_subscription_client(lohra_home())
        except SubscriptionError as exc:
            print(f"subscription mode: {exc}", file=sys.stderr)
            return None, None, 2
        print("⚠️  dashboard using your OpenAI/Codex subscription (opt-in, ToS-gray).", file=sys.stderr)
    else:
        profile, code = _resolve_profile(None)
        if profile is None:
            return None, None, code
        model_override = os.environ.get("LOHRA_MODEL")

    # No --model flag here; allow LOHRA_MODEL (or the codex default) to override.
    chosen_model = _resolve_model(profile, model_override)
    if chosen_model is None:
        print(
            f"provider {profile.name!r} has no default model — set LOHRA_MODEL.",
            file=sys.stderr,
        )
        return None, None, 2
    try:
        # ONE client shared across sessions — the SDK/httpx client is thread-safe
        # and meant to be reused, avoiding a per-session connection-pool leak.
        if shared_client is None:
            shared_client = build_client(profile)
    except Exception as exc:
        print(f"could not initialize the {profile.name} client: {exc}", file=sys.stderr)
        return None, None, 2

    register_all_tools()
    # Connect MCP servers once at startup, before snapshotting the shared tool
    # definitions every session freezes into its prompt.
    mcp_manager = register_configured_mcp_servers(registry)
    tool_definitions = tuple(registry.get_definitions())
    home = lohra_home()
    db = SessionDB(str(state_db_path()))
    cron_store = CronStore(home)
    # One vision runner, shared: it only closes over the stable client/transport/model.
    vision_runner = make_vision_runner(
        shared_client, get_transport(profile.api_mode), chosen_model
    )
    image_gen_runner = make_image_gen_runner(shared_client, str(home / "images"))
    # One shared orchestration core for the app. Sub-sessions use the isolated
    # subagent factory, parented to the session whose agent spawned them.
    # Limits from LOHRA_MAX_PARALLEL / LOHRA_MAX_SUBSESSIONS env (no flag in the
    # dashboard, like LOHRA_MODEL).
    max_concurrent, max_children = resolve_limits()
    orchestration_core = OrchestrationCore(
        db,
        make_child_factory(
            model=chosen_model,
            provider=profile,
            client=shared_client,
            tool_definitions=tool_definitions,
        ),
        max_concurrent=max_concurrent,
        max_children=max_children,
    )
    # Cross-provider delegation pool (borrows the shared client; closed in _cleanup).
    client_pool = ClientPool(parent_provider=profile, parent_client=shared_client, home=home)
    workflow_service = WorkflowService(
        base_child_factory=make_child_factory(
            model=chosen_model,
            provider=profile,
            client=shared_client,
            tool_definitions=tool_definitions,
        ),
        db=db,
        home=home,
        client_pool=client_pool,
    )

    # Project context discovered once from the dashboard's launch cwd (best-effort;
    # the desktop launches from its bundle, a real project only when run from one).
    project_context, env_hints = load_project_context(Path(os.getcwd()))
    skill_roots = discover_skill_roots(Path(os.getcwd()))

    def agent_factory(session_id: str):
        # Each session gets its own stores so its memory/skills snapshot freezes
        # independently (Invariante #1); they share the same on-disk home + db.
        # The session id is threaded into the dispatch so the tools bound to it
        # know who they belong to: run_workflow stamps it as the run's owner (so
        # the finished run lands in THIS session's steer inbox) and the
        # orchestration tools use it as the parent of the sub-sessions they spawn.
        memory_store, skill_store = build_session_stores(home, skill_roots)
        return Agent(
            model=chosen_model,
            provider=profile,
            client=shared_client,
            identity=load_soul(home),
            context_files=project_context,
            environment_hints=env_hints,
            tool_definitions=tool_definitions,
            tool_dispatch=build_session_dispatch(
                memory_store,
                skill_store,
                db,
                cron_store,
                vision_runner,
                image_gen_runner,
                orchestration_core,
                session_id,
                workflow_service,
                client_pool=client_pool,
            ),
            memory_store=memory_store,
            skill_store=skill_store,
            max_iterations=PARENT_MAX_ITERATIONS,
        )

    # Background scheduler: each due job runs as a fresh, tool-less relay agent.
    cron_stop = threading.Event()
    cron_runner = make_cron_runner(
        lambda: Agent(model=chosen_model, provider=profile, client=shared_client), db=db
    )
    threading.Thread(
        target=run_scheduler_loop, args=(cron_store, cron_runner),
        kwargs={"stop": cron_stop}, daemon=True,
    ).start()

    manager = SessionManager(db, agent_factory)
    # A finished run announces itself in the steer inbox of the session that
    # launched it — never in the frozen system prompt (Invariante #1). Runs
    # launched by a session the manager can't resolve are a silent no-op.
    bind_workflow_notifier(workflow_service, manager.get)
    # The desktop shell mints the token and passes it via env so both sides
    # agree; standalone use generates one. --insecure disables auth entirely.
    if insecure:
        token = None
    else:
        token = os.environ.get("LOHRA_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(manager, token=token)

    def _cleanup() -> None:
        cron_stop.set()
        workflow_service.shutdown()
        orchestration_core.shutdown()
        client_pool.close()  # close only the cross-provider clients it built
        if mcp_manager is not None:
            mcp_manager.shutdown()
        db.close()
        shared_client.close()

    app.state.cleanup = _cleanup
    return manager, app, token


def _run_auth_login(home) -> int:
    """Run the OAuth device flow and store Lohra's own (auto-refreshing) token."""
    from lohra.subscription import oauth, token_store

    try:
        device = oauth.start_device_login(oauth.default_post)
    except oauth.OAuthError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"\nTo log in, open:\n  {device.verify_url}\nand enter the code:\n  {device.user_code}\n\n"
        "Waiting for authorization (Ctrl-C to cancel)...",
        file=sys.stderr,
    )
    try:
        tokens = oauth.poll_for_tokens(device, oauth.default_post)
    except oauth.OAuthError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nlogin cancelled.", file=sys.stderr)
        return 1
    token_store.write_tokens(home, tokens)
    print("logged in — Lohra now holds its own token and will refresh it automatically.")
    return 0


def run_auth(action: str, *, assume_yes: bool = False) -> int:
    """`lohra auth status|enable|disable` — manage OpenAI/Codex subscription mode."""
    import json as _json

    from lohra.memory.paths import lohra_home
    from lohra.subscription import manage

    home = lohra_home()
    if action == "status":
        print(_json.dumps(manage.status(home), indent=2))
        return 0
    if action == "disable":
        manage.disable(home)
        print("subscription mode disabled — using API key.")
        return 0
    if action == "logout":
        from lohra.subscription import token_store

        removed = token_store.clear_tokens(home)
        print("logged out (own OAuth token removed)." if removed else "no own login to remove.")
        return 0
    if action == "login":
        return _run_auth_login(home)
    # enable: show the ToS warning and require explicit confirmation
    print(manage.TOS_WARNING, file=sys.stderr)
    if not assume_yes:
        sys.stderr.write("\nEnable subscription mode anyway? [y/N]: ")
        sys.stderr.flush()
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("aborted — subscription mode NOT enabled.")
            return 1
    manage.enable(home)
    print(
        "subscription mode enabled (OpenAI/Codex). Now log in with one of:\n"
        "  lohra auth login   — Lohra's own login (auto-refreshing; recommended)\n"
        "  codex login        — reuse your Codex CLI login (no auto-refresh)"
    )
    return 0


def run_dashboard(*, host: str, port: int, insecure: bool = False) -> int:
    """Serve the FastAPI dashboard (WS + REST) on host:port."""
    import uvicorn

    _, app, token = build_dashboard_app(insecure=insecure)
    if app is None:
        return token  # token holds the exit code on failure
    suffix = "" if insecure else f"?token={token}"
    print(f"Lohra dashboard: http://{host}:{port}", file=sys.stderr)
    print(f"WebSocket:       ws://{host}:{port}/api/ws{suffix}", file=sys.stderr)
    try:
        # lifespan="off": the app registers no startup/shutdown handlers, and the
        # ASGI lifespan handshake deadlocks inside a PyInstaller-frozen binary —
        # turning it off is lossless here and lets the packaged backend serve.
        uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    finally:
        app.state.cleanup()
    return 0


def build_openai_server_app(*, insecure: bool, tools: str = ""):
    """Build (app, api_key) for the OpenAI server, or (None, exit_code) on failure.

    Relay mode (default): a fresh, tool-less Agent per request against the
    configured provider (the request's ``model`` is honored). When ``tools`` is a
    non-empty comma-separated allow-list, agentic mode runs those tools
    server-side (the subagent guards apply). Separated from serving so the wiring
    is testable without uvicorn.
    """
    import os
    import secrets

    from lohra.agent import Agent
    from lohra.agent.agent import DEFAULT_MAX_ITERATIONS
    from lohra.agent.client import build_client
    from lohra.agent.delegate import PARENT_MAX_ITERATIONS
    from lohra.server.agentic import build_allowed_tools
    from lohra.server.app import create_openai_app
    from lohra.server.service import CompletionService

    profile, code = _resolve_profile(None)
    if profile is None:
        return None, code
    try:
        # One client shared across requests (thread-safe, pooled).
        shared_client = build_client(profile)
    except Exception as exc:
        print(f"could not initialize the {profile.name} client: {exc}", file=sys.stderr)
        return None, 2

    allowed = [name.strip() for name in tools.split(",") if name.strip()]
    tool_definitions, tool_dispatch = (build_allowed_tools(allowed) if allowed else ((), None))
    if allowed:
        names = ", ".join(d["function"]["name"] for d in tool_definitions)
        print(f"⚠️  agentic mode — server-side tools enabled: {names or '(none matched)'}", file=sys.stderr)
        print(
            "    these run with the server's privileges and are NOT sandboxed; "
            "'terminal'/'write_file' over HTTP are remote code execution.",
            file=sys.stderr,
        )
        if insecure:
            print(
                "    ⚠️  --insecure with tools = UNAUTHENTICATED remote code execution.",
                file=sys.stderr,
            )

    def agent_factory():
        # The request's model is applied by the service per request.
        return Agent(
            model=profile.fallback_models[0] if profile.fallback_models else "",
            provider=profile,
            client=shared_client,
            tool_definitions=tool_definitions,
            tool_dispatch=tool_dispatch,
            max_iterations=PARENT_MAX_ITERATIONS if tool_definitions else DEFAULT_MAX_ITERATIONS,
        )

    service = CompletionService(agent_factory)
    api_key = None if insecure else (
        os.environ.get("LOHRA_OPENAI_API_KEY") or secrets.token_urlsafe(32)
    )
    app = create_openai_app(service, api_key=api_key, models=tuple(profile.fallback_models))
    app.state.cleanup = shared_client.close
    return app, api_key


def run_openai_server(*, host: str, port: int, insecure: bool = False, tools: str = "") -> int:
    """Serve the OpenAI-compatible API on host:port."""
    import uvicorn

    from lohra.memory.paths import lohra_home
    from lohra.subscription.credentials import subscription_active

    # Gate (Fase 10): NEVER back Lohra's own server with a subscription token — it
    # would expose the user's ChatGPT/Codex subscription to every server client.
    if subscription_active(lohra_home()):
        print(
            "refusing to serve: subscription mode is active, and relaying your "
            "ChatGPT/Codex subscription through this server would expose it. "
            "Run `lohra auth disable` (or use an API key) to serve.",
            file=sys.stderr,
        )
        return 2

    app, api_key = build_openai_server_app(insecure=insecure, tools=tools)
    if app is None:
        return api_key  # holds the exit code on failure
    print(f"Lohra OpenAI server: http://{host}:{port}/v1", file=sys.stderr)
    if api_key:
        print(f"API key: {api_key}", file=sys.stderr)
    try:
        # lifespan="off": no startup/shutdown handlers, and the lifespan handshake
        # deadlocks in a PyInstaller-frozen binary — see run_dashboard.
        uvicorn.run(app, host=host, port=port, log_level="warning", lifespan="off")
    finally:
        app.state.cleanup()
    return 0


def run_workflow_cmd(
    action: str,
    *,
    run_id: str | None = None,
    last: bool = False,
    limit: int = 20,
    poll: float = 2.0,
    sleep=None,
) -> int:
    """Look at workflow runs from the shell — list them, or follow one.

    Reads the DURABLE run state only (the same rows ``workflow_status`` falls
    back to across processes), so it needs no provider, no API key and no agent:
    a human can watch a run the agent launched in another terminal without
    spending a token. ``sleep`` is injected so the poll loop is testable.
    """
    import time

    from lohra.memory.paths import state_db_path
    from lohra.state import SessionDB
    from lohra.workflow import watch as watchlib
    from lohra.workflow.runstate_store import RunStateStore

    db = SessionDB(str(state_db_path()))
    try:
        return watchlib.run_command(
            action,
            db=db,
            store=RunStateStore(db),
            sleep=sleep if sleep is not None else time.sleep,
            write=print,
            warn=lambda line: print(line, file=sys.stderr),
            run_id=run_id,
            last=last,
            limit=limit,
            poll=poll,
        )
    finally:
        db.close()


def run_cron(
    action: str,
    *,
    job_id: str | None = None,
    name: str | None = None,
    prompt: str | None = None,
    interval: int | None = None,
    cron_expr: str | None = None,
    at: float | None = None,
) -> int:
    """Manage scheduled jobs (list/add/remove/pause/resume) from the CLI."""
    from lohra.cron.store import CronError, CronStore
    from lohra.memory.paths import lohra_home

    store = CronStore(lohra_home())

    if action == "list":
        jobs = store.list()
        if not jobs:
            print("no scheduled jobs")
        for job in jobs:
            state = "on" if job["enabled"] else "paused"
            print(f"{job['id']}  [{state}] {job['name']}  ({job['type']}={job['value']})")
        return 0

    if action == "add":
        if interval is not None:
            schedule_type, value = "interval", interval
        elif cron_expr is not None:
            schedule_type, value = "cron", cron_expr
        elif at is not None:
            schedule_type, value = "once", at
        else:
            print("add needs one of --interval, --cron, or --at", file=sys.stderr)
            return 2
        try:
            job = store.add(name=name or "", prompt=prompt or "", type=schedule_type, value=value)
        except CronError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"added job {job['id']}")
        return 0

    if not job_id:
        print(f"{action} needs a job id", file=sys.stderr)
        return 2
    ok = store.remove(job_id) if action == "remove" else store.set_enabled(job_id, action == "resume")
    if not ok:
        print(f"no job with id {job_id!r}", file=sys.stderr)
        return 1
    print(f"{action} {job_id}")
    return 0


def run_profile(action: str, *, name: str | None = None) -> int:
    """List existing workspaces, or create a new one (creates its dir layout)."""
    from lohra.memory.paths import (
        ensure_home,
        list_profiles,
        validate_profile_name,
    )

    if action == "list":
        profiles = list_profiles()
        active = os.environ.get("LOHRA_PROFILE") or None
        if not profiles:
            print("no profiles yet — create one with `lohra profile create <name>`")
            return 0
        for profile in profiles:
            print(f"* {profile}" if profile == active else f"  {profile}")
        return 0

    # create
    if not name:
        print("profile create needs a name", file=sys.stderr)
        return 2
    try:
        validate_profile_name(name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    os.environ["LOHRA_PROFILE"] = name  # so ensure_home() roots under this profile
    home = ensure_home()
    print(f"created profile {name!r} at {home}")
    return 0


def run_update(*, check: bool = False, reinstall: bool = False) -> int:
    """Pull the latest backend code from its git checkout (Phase 6)."""
    from lohra.selfupdate import service

    repo = service.resolve_repo()
    if repo is None:
        print(
            "cannot self-update: not a git checkout.\n"
            "  installed from a wheel? upgrade with:  pip install --upgrade lohra\n"
            "  (or re-run however you installed it: uv tool upgrade lohra / pipx upgrade lohra)",
            file=sys.stderr,
        )
        return 2

    if check:
        result = service.check_update(repo)
        print(result.message)
        return 0 if result.ok else 2

    result = service.perform_update(repo)
    print(result.message)
    if not result.ok:
        return 2
    if reinstall:
        # Reinstall the freshly-pulled tree (independent of git state).
        print("reinstalling…")
        ok, output = service.reinstall(repo)
        if not ok:
            print(f"reinstall failed:\n{output}", file=sys.stderr)
            return 2
        print("reinstalled.")
    elif result.reinstall_recommended:
        # The pull already landed; the remedy is to reinstall the current tree —
        # NOT to re-run `lohra update` (that would find nothing to pull).
        print(f"dependencies changed — apply with: pip install -e {repo / 'backend'}")
    if result.restart_required:
        print("restart lohra to apply the update.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        print(f"lohra {__version__} — see `lohra --help`")
        return 0
    # Activate the workspace profile ONCE, before any path is resolved, by
    # setting LOHRA_PROFILE — every subsystem reads its home through it. Validate
    # both the flag and any out-of-band LOHRA_PROFILE here, so a bad value fails
    # fast with exit 2 instead of a traceback deep in path resolution.
    from lohra.memory.paths import active_profile

    if getattr(args, "profile", None):
        os.environ["LOHRA_PROFILE"] = args.profile
    try:
        active_profile()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # Load API keys from ~/.lohra/.env (base, profile-independent) before any
    # provider resolution — a Finder-launched app has no shell env, so this is
    # how the packaged backend gets its key. Real env vars still win.
    from lohra.config.env_file import apply_env_file
    from lohra.memory.paths import lohra_base

    apply_env_file(lohra_base() / ".env")
    if args.command == "auth":
        return run_auth(args.action, assume_yes=args.yes)
    if args.command == "profile":
        return run_profile(args.action, name=args.name)
    if args.command == "skill":
        from pathlib import Path as _P

        from lohra.skills.exportkit import read_exportable, write_exportable

        try:
            if args.to:
                out = write_exportable(args.name, _P(args.to))
                print(f"wrote {out}")
            else:
                print(read_exportable(args.name), end="")
        except KeyError as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 2
        return 0

    if args.command == "update":
        return run_update(check=args.check, reinstall=args.reinstall)
    if args.command == "chat":
        return run_chat(
            args.prompt,
            model=args.model,
            provider=args.provider,
            session=args.session,
            use_tools=not args.no_tools,
            yolo=args.yolo,
            max_parallel=args.max_parallel,
            json_output=args.json_output,
        )
    if args.command == "dashboard":
        return run_dashboard(host=args.host, port=args.port, insecure=args.insecure)
    if args.command == "serve":
        return run_openai_server(
            host=args.host, port=args.port, insecure=args.insecure, tools=args.tools
        )
    if args.command == "workflow":
        return run_workflow_cmd(
            args.workflow_cmd,
            run_id=getattr(args, "run_id", None),
            last=getattr(args, "last", False),
            limit=getattr(args, "limit", 20),
            poll=getattr(args, "poll", 2.0),
        )
    if args.command == "cron":
        return run_cron(
            args.action,
            job_id=args.job_id,
            name=args.name,
            prompt=args.prompt,
            interval=args.interval,
            cron_expr=args.cron_expr,
            at=args.at,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
