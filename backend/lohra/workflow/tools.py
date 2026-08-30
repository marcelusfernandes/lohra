"""The agent's workflow tool surface (spec §3) — intercepted, like delegate_task.

``run_workflow`` lets the agent author a declarative workflow spec and run it
autonomously in the background (returns a run_id immediately); ``workflow_status``
polls the rollup (including live per-node ``progress``); ``workflow_list`` shows
every run at once; ``workflow_pause`` stops one resumably; ``workflow_cancel``
aborts. Bound per session to a WorkflowService. Excluded from subagents and the
server (a workflow leaf must never launch more workflows).
"""

from __future__ import annotations

import json
from typing import Any

from lohra.tools.registry import registry, tool_error, tool_result
from lohra.workflow.service import WorkflowService

# A minimal spec the author can copy: run input ref, a validated leaf, an
# adversarial check on it, a dependent synthesis. Rendered INTO the guidance
# below (single source) so the shown text is always a spec that validates.
EXAMPLE_SPEC = {
    "meta": {"name": "triage-bugs"},
    "schemas": {"FINDING": {"type": "object", "properties": {"bug": {"type": "string"}}}},
    "nodes": [
        {
            "id": "scan",
            "type": "agent",
            "prompt": "Name the worst bug in ${args.dump}.",
            "schema_ref": "FINDING",
        },
        {"id": "check", "type": "verify", "finding": "${scan.bug}", "skeptics": 3},
        {
            "id": "report",
            "type": "agent",
            "depends_on": ["check"],
            "prompt": "Write a fix plan for ${check.finding}.",
        },
    ],
}

RUN_GUIDANCE = (
    "Run a dynamic multi-agent workflow you author as a declarative spec — a DAG "
    "of sub-agents that pursues a goal autonomously. 'spec' is an object with "
    "meta{name}, optional schemas{NAME: <json-schema>}, and a 'nodes' list; every "
    "node is {id, type, ...fields}. The node types are a CLOSED set of 10:\n"
    "- agent: one leaf prompt. Add 'schema' (inline JSON-Schema) or 'schema_ref' "
    "(a name from schemas) to get validated JSON back instead of prose.\n"
    "- parallel: barrier fan-out over 'branches' (a list of nodes) — all of them "
    "finish before anything downstream runs.\n"
    "- pipeline: 'items' (a list or a ${ref}) x 'stages' (agent-shaped stages). "
    "Each item flows through the stages on its own, with no barrier between "
    "items — use this, not parallel, for per-item processing.\n"
    "- loop_until_dry: repeat 'body' until 'stop_after_k_empty' rounds come back "
    "empty, capped by 'max_rounds'.\n"
    "- verify: adversarial check — 'skeptics' sub-agents try to refute 'finding' "
    "(optional 'lenses'); a majority refutation kills it.\n"
    "- judge_panel: 'attempts' are scored by 'judges' independent judges, and the "
    "winner is rewritten by the 'synthesize' prompt.\n"
    "- workflow: run a saved template by 'ref' as a nested sub-workflow.\n"
    "- gate: draft 'body' (agent-shaped), have a reviewer leaf judge it against "
    "'validator', and re-draft with its feedback until it passes ('attempts', "
    "default 2) — the cheap way to hold one answer to a standard.\n"
    "- completeness_check: audit 'results' against 'task'; returns "
    "{complete, missing} — pair it with loop_until_dry to keep digging.\n"
    "- checkpoint: ask a HUMAN 'prompt' and PAUSE the run (it spawns nothing); "
    "resume with checkpoint_answers={id: answer} where EVERY answer was supplied "
    "verbatim by that human. A 'default' auto-answers the gate on a plain resume, "
    "so author one ONLY when the human explicitly gave you that default before "
    "the run — the agent never invents a default or an answer.\n"
    "Agent and rigor nodes (verify, judge_panel, loop_until_dry, gate, "
    "completeness_check) may name a portable 'tier' (small|medium|big) instead "
    "of a 'model' slug — the operator maps it, and one resolved routing applies "
    "to every leaf the node spawns. An explicit 'model' wins over the tier. Put "
    "the knobs on the NODE: one level down, inside 'body'/'synthesize'/"
    "'branches'/'stages', a routing knob is silently ignored (no error, no "
    "fault) and those leaves bill the session's own model.\n"
    "Before naming a 'model' or a 'provider' on a node, call list_models — it "
    "reports what is REACHABLE right now plus the operator's tier map. Never "
    "invent a slug: only 'tier' is a closed enum, and 'model'/'effort'/'provider' "
    "are free fields nothing validates — the catalog is information, not an "
    "allow-list. Nodes in the SAME DAG may name DIFFERENT providers, including "
    "'openai-codex' (the subscription — refused unless the human enabled it AND "
    "prefers it, and a refused node just comes back null) beside an API-key one. "
    "If the user asked to CONFIRM the assignment, put a checkpoint presenting "
    "the plan (node -> model/provider) before the expensive nodes; on automatic, "
    "assign straight from the tiers and the catalog and don't stop to ask.\n"
    "A leaf (or pipeline stage) that dies with 'max_iterations (N) reached' needs "
    "a bigger 'max_iterations' (1-128, default 50), not a longer 'timeout'. 128 "
    "is the ceiling the harness already enforces on anything authored; the "
    "raise-once formula in the supervision limits below applies ONLY when "
    "N < 128 — a leaf that hit the ceiling gets NO resume, it escalates to a "
    "human.\n"
    "Reference an earlier node's output with ${node.field} and the run inputs "
    "with ${args.x} — plain dotted paths only, never expressions. Use "
    "'depends_on' to order nodes that share no data ref.\n"
    f"A complete valid spec: {json.dumps(EXAMPLE_SPEC, separators=(',', ':'))}\n"
    "Returns a run_id immediately — poll it with workflow_status ('wait' blocks) "
    "and abort with workflow_cancel. Reach for verify nodes for adversarial "
    "checking and agent schemas for structured output. TIP: call "
    "workflow_templates FIRST — adapt a proven template instead of authoring "
    "from scratch whenever one fits the task shape.\n"
    "SUPERVISION DOCTRINE (behavioural, not enforced — nothing in the harness checks these; the workflow-authoring skill holds the full table and rationale, so consult it rather than improvising):\n"
    "When a run stops or stalls, work one loop — watch -> diagnose -> adapt -> resume. Record a workaround like the human-quality judgement it is: BEFORE adapting, write to the trace/log the diagnosis, the key (run, cause, target), the change you are making, the PRE-workaround progress fingerprint and an estimate of its incremental cost; AFTER it settles, record the outcome, the progress fingerprint and the cost actually incurred. The record is a compact supervision note in the current conversation plus the ensuing tool calls; if that record is unavailable after a handoff or restart, do not reconstruct it — escalate. Every autonomous adaptation must be REVERSIBLE, budgeted and recorded — anything that fails one of those (or is irreversible outright) is a human call. Adaptation is capped: at most ONE attempt per (run, cause, target) pair, and at most 3 per run.\n"
    "The K=2 brake is RUN-LEVEL: one workaround is no-progress only when its POST-workaround fingerprint equals its own PRE-fingerprint; two SUCCESSIVE no-progress workarounds — whether or not they share a (run, cause, target) key — open a GLOBAL brake for that run: stop adapting and escalate to a human. Polls taken while the run is still running don't count; only settled workarounds do. The per-key cap stays at one attempt.\n"
    "A bad model SLUG may be corrected automatically: ONLY after list_models, staying on the SAME provider and credential/billing route, and go ahead only when there is evidence that the route is a fixed-price subscription, OR when pricing metadata (or the operator's preauthorization) shows the new cost is not higher. The current list_models does NOT report prices, so an API-key route with no operator preauthorization ESCALATES to a human instead of swapping. An unsupported OPTIONAL provider parameter (e.g. 'effort') may be dropped or corrected only when the user never required it and removing it does not change the goal — otherwise a human decides. Crossing to another provider or billing route is ALWAYS a human call.\n"
    "max_iterations (N) may be raised once to min(N+4, 128), never more — and only when N < 128: at the 128 ceiling there is no resume, escalate to a human. A non-quota transient provider failure may get one resume only after its cooldown and only when no auto-resume is pending. On a "
    "quota_exhausted pause, respect its resume_at: if it is in the future, wait it out and never launch a competing resume; if it is past, poll once and escalate if still paused; if resume_at is null or attempts are exhausted, escalate to a human. ALWAYS HUMAN, never automatic: any increase to a run's token budget, checkpoint answers, credentials, permissions, scope, irreversible actions, and any change to provider or billing route.\n"
    "Re-running is cheap: run_workflow(resume_run_id=...) replays the cells that "
    "already completed and only re-spawns what died. A 'paused' status means the "
    "run stopped RESUMABLY, not that the spec failed — it keeps its finished "
    "nodes. Spent 'token_budget' (the optional cap on what the whole run may "
    "spend, reported back as {total, spent, remaining}) is a human decision, "
    "never an agent one.\n"
    "While a run is in flight you can always look: workflow_status reports live "
    "'progress' per node, workflow_list shows every run at once, and "
    "workflow_pause stops one resumably (nothing in flight is thrown away).\n"
    "For choosing between the node types, sizing the fan-out and reading the "
    "rollup honestly, load the workflow-authoring skill first."
)
TEMPLATES_GUIDANCE = (
    "List validated workflow templates (proven specs to adapt), or fetch one by "
    "'name' to get its full spec. Prefer adapting a template over authoring fresh."
)

_SPEC_PARAM = {
    "type": "object",
    "description": (
        "The workflow spec: {meta:{name}, schemas?, nodes:[{id, type, ...}]}. "
        "type is one of: agent, parallel, pipeline, loop_until_dry, verify, "
        "judge_panel, workflow, gate, completeness_check, checkpoint (see this "
        "tool's description for their fields)."
    ),
}
_RUN_SCHEMA = {
    "description": RUN_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "spec": _SPEC_PARAM,
            "args": {
                "type": "object",
                "description": (
                    "Inputs for the run (referenced as ${args.x}). A resume "
                    "replays the run's OWN args — send these again only to "
                    "change them."
                ),
            },
            "resume_run_id": {
                "type": "string",
                "description": (
                    "Re-run a prior run_id, reusing its cached cells (resume after "
                    "a crash). It replays the run's OWN persisted spec, so 'spec' "
                    "is optional here — send one only to run something different. "
                    "The spec, args and pending checkpoint are on disk, so this "
                    "works in a later session too, not just this one."
                ),
            },
            "checkpoint_answers": {
                "type": "object",
                "description": (
                    "Answers for the 'checkpoint' nodes a previous stretch of "
                    'this run paused on, keyed by node id: {"approve": "yes"}. '
                    "Every answer MUST be one the HUMAN supplied verbatim — the "
                    "agent never infers, paraphrases or invents one, and never "
                    "manufactures a 'default' answer of its own. Each answer "
                    "becomes that node's output and is cached, so the same "
                    "question is never asked twice."
                ),
            },
            "token_budget": {
                "type": "integer",
                "description": (
                    "Cap the tokens this whole run may spend. Checked before every "
                    "leaf spawn; overrunning pauses the run instead of truncating it. "
                    "Raising it on a resume requires HUMAN authorization — never do "
                    "it on your own judgment. Omit it to inherit the run's original cap."
                ),
            },
        },
        # 'spec' is NOT required: a resume replays the run's persisted spec.
        "required": [],
    },
}
_STATUS_SCHEMA = {
    "description": (
        "Poll a workflow run's status/outputs by its run_id. 'wait' blocks until done. "
        "'progress' is live even mid-run — {done, running, pending, total} plus a "
        "per-node list (pending/running/complete/null, and settled items for a "
        "pipeline) — so a long run is never a black box. "
        "status 'paused' means the run stopped resumably, not that the spec failed: the "
        "reply carries reason/resume_at/attempts and the finished nodes are kept. "
        "reason 'quota_exhausted' (the provider) distinguishes two cases via 'resume_at': "
        "if resume_at is set, wait it out — do not compete with the run's own "
        "auto-resume; if resume_at is null (or its retries are exhausted), escalate to a "
        "human. reason 'token_budget_exhausted' never retries: the budget is a HUMAN "
        "decision — report the available token_budget/spend fields and the case for more. "
        "reason 'checkpoint' pauses for the HUMAN: the reply carries "
        "checkpoint{node_id, prompt, default?} — relay the question, get the human's "
        "answer, and pass it back with "
        "run_workflow(resume_run_id=..., checkpoint_answers={node_id: answer}); never "
        "author an answer or a default yourself. "
        "A run still marked 'running' with 'stale' true is one whose process was "
        "lost — the agent may resume it under the supervision brakes; its finished "
        "cells replay."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "wait": {
                "type": "boolean",
                "description": "Block until the run finishes (default false)",
            },
        },
        "required": ["run_id"],
    },
}
_CANCEL_SCHEMA = {
    "description": "Cancel a running workflow by its run_id.",
    "parameters": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
}
_LIST_SCHEMA = {
    "description": (
        "List the workflow runs this session knows (newest first): run_id, name, "
        "status, nodes_done/nodes_total, tokens_spent and token_budget. Use it to "
        "find a run whose id you lost, or to see what is still in flight before "
        "starting another one."
    ),
    "parameters": {"type": "object", "properties": {}},
}
_PAUSE_SCHEMA = {
    "description": (
        "Pause a running workflow by its run_id — the resumable stop. Unlike "
        "workflow_cancel, nothing is thrown away: leaves already in flight finish "
        "and are charged, finished nodes are kept, and the run reports status "
        "'paused' with reason 'user_requested'. Nothing resumes it on its own — "
        "continue it whenever you like with run_workflow(resume_run_id=...), no "
        "token_budget raise needed."
    ),
    "parameters": {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    },
}
_TEMPLATES_SCHEMA = {
    "description": TEMPLATES_GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Fetch this template's full spec (omit to list)",
            }
        },
    },
}

_STATUS_TIMEOUT = 600.0


class WorkflowTool:
    """Binds the workflow tools to a session's WorkflowService.

    ``taint`` (optional) is the session's TaintTracker: if the authoring turn
    ingested web/MCP content, runs spawn with reduced leaf capability (§8.2).

    ``owner`` (optional) is this session's id, stamped on every run it launches so
    a finished run can be announced in this session's steer inbox (M6)."""

    def __init__(
        self,
        service: WorkflowService,
        *,
        taint: Any | None = None,
        owner: str | None = None,
    ) -> None:
        self._service = service
        self._taint = taint
        self._owner = owner

    def run(self, args: dict[str, Any]) -> str:
        spec = args.get("spec")
        resume_run_id = args.get("resume_run_id")
        if spec is not None and not isinstance(spec, dict):
            return tool_error("'spec' must be an object (with meta + nodes)")
        if spec is None and not resume_run_id:
            # A resume replays the spec the run persisted (WF-22) — which is what
            # this tool's own guidance has always promised. A FRESH run has
            # nothing to replay, so it still needs one.
            return tool_error("run_workflow needs a 'spec' object (with meta + nodes)")
        answers = args.get("checkpoint_answers")
        if answers is not None and not isinstance(answers, dict):
            return tool_error("'checkpoint_answers' must be an object keyed by checkpoint node id")
        run_args = args.get("args")
        if run_args is not None and not isinstance(run_args, dict):
            return tool_error("'args' must be an object of run inputs (referenced as ${args.x})")
        out = self._service.start(
            spec,
            # Forwarded RAW: an `or {}` here would turn "the caller sent no
            # args" into "run with no args" before the service can tell, and a
            # resume would silently drop the inputs the run persisted (WF-24).
            run_args,
            resume_run_id=args.get("resume_run_id"),
            checkpoint_answers=answers,
            token_budget=args.get("token_budget"),
            tainted=bool(self._taint and self._taint.tainted),
            owner=self._owner,
        )
        if "error" in out:
            # Only a spec-validation failure is a SPEC problem. Labelling a
            # too-small token_budget (or a resume onto a live run) "invalid
            # workflow spec" sends the author rewriting a spec that was fine.
            if out.get("invalid_spec"):
                return tool_error(f"invalid workflow spec: {out['error']}")
            return tool_error(out["error"])
        return tool_result(**out)

    def status(self, args: dict[str, Any]) -> str:
        run_id = args.get("run_id")
        if not run_id:
            return tool_error("workflow_status needs a 'run_id'")
        out = self._service.status(
            str(run_id), wait=bool(args.get("wait")), timeout=_STATUS_TIMEOUT
        )
        return tool_error(out["error"]) if "error" in out else tool_result(**out)

    def list(self, args: dict[str, Any]) -> str:
        return tool_result(runs=self._service.list_runs())

    def pause(self, args: dict[str, Any]) -> str:
        run_id = args.get("run_id")
        if not run_id:
            return tool_error("workflow_pause needs a 'run_id'")
        out = self._service.pause(str(run_id))
        return tool_error(out["error"]) if "error" in out else tool_result(**out)

    def cancel(self, args: dict[str, Any]) -> str:
        run_id = args.get("run_id")
        if not run_id:
            return tool_error("workflow_cancel needs a 'run_id'")
        out = self._service.cancel(str(run_id))
        return tool_error(out["error"]) if "error" in out else tool_result(**out)

    def templates(self, args: dict[str, Any]) -> str:
        name = args.get("name")
        if name:
            spec = self._service.get_template(str(name))
            if spec is None:
                return tool_error(f"no template named {name!r}")
            return tool_result(name=str(name), spec=spec)
        # List proven templates + recent failure priors (what shapes to avoid).
        return tool_result(
            templates=self._service.list_templates(),
            insights=self._service.recent_insights(),
        )


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("workflow tools must be intercepted with a session WorkflowService")


def register_workflow_tool_schemas() -> None:
    """Register the workflow tool schemas (execution is intercepted)."""
    registry.register(
        "run_workflow", "workflow", _RUN_SCHEMA, _intercepted, override=True, emoji="🕸️"
    )
    registry.register(
        "workflow_status", "workflow", _STATUS_SCHEMA, _intercepted, override=True, emoji="📊"
    )
    registry.register(
        "workflow_list", "workflow", _LIST_SCHEMA, _intercepted, override=True, emoji="📋"
    )
    registry.register(
        "workflow_pause", "workflow", _PAUSE_SCHEMA, _intercepted, override=True, emoji="⏸️"
    )
    registry.register(
        "workflow_cancel", "workflow", _CANCEL_SCHEMA, _intercepted, override=True, emoji="🛑"
    )
    registry.register(
        "workflow_templates", "workflow", _TEMPLATES_SCHEMA, _intercepted, override=True, emoji="📚"
    )
