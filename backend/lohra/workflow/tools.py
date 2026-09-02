"""The agent's workflow tool surface (spec §3) — intercepted, like delegate_task.

``run_workflow`` lets the agent author a declarative workflow spec and run it
autonomously in the background (returns a run_id immediately); ``workflow_status``
polls the rollup (including live per-node ``progress``); ``workflow_list`` shows
every run at once; ``workflow_pause`` stops one resumably; ``workflow_cancel``
aborts; ``workflow_steer`` queues an instruction into a live run's leaf.
Bound per session to a WorkflowService. Excluded from subagents and the
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
    "A spec with more than one node and NO 'depends_on' or ${ref} ANYWHERE "
    "still validates — but the engine runs them ONE AT A TIME, in a queue, with "
    "no relation between them, so this comes back with a 'warnings' entry "
    "instead of blocking; fix it by adding 'depends_on'/${ref} or grouping the "
    "unrelated nodes as branches of a 'parallel' node.\n"
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
    "max_iterations (N) may be raised once to min(N+4, 128), never more — and only when N < 128: at the 128 ceiling there is no resume, escalate to a human. A non-quota transient provider failure may get one resume only after its cooldown and only when no auto-resume is pending. "
    "A non-quota provider death already burned that node's 'retries' on the SAME route before it nulled (that knob covers an empty answer and a generic provider error; never a quota pause, either timeout, or a cancel), so your resume is the second attempt at the SHAPE, not the first at the route.\n"
    "On a "
    "quota_exhausted pause, respect its resume_at: if it is in the future, wait it out and never launch a competing resume; if it is past, poll once and escalate if still paused; if resume_at is null or attempts are exhausted, escalate to a human. ALWAYS HUMAN, never automatic: any increase to a run's token budget, checkpoint answers, credentials, permissions, scope, irreversible actions, and any change to provider or billing route.\n"
    "ADAPTED-SPEC PIVOT: for a settled run whose route is wrong, send an explicit "
    "adapted spec with the same resume_run_id; preserve meta.name and meta.version "
    "and change only affected node fields. Completed unchanged cells replay; a new run_id reuses NO cells. "
    "Do this automatically only on the same provider and credential/billing route, after catalog validation and "
    "the SUP-01 cost qualification; never raise token_budget. Changing provider or billing route is a human call. "
    "A quota run with resume_at in the future is not a pivot candidate: its auto-resume owns the wait, so never "
    "launch a competing resume. Record the diagnosis, before/after fingerprint, changed fields, cache reuse and "
    "incremental cost like every workaround.\n"
    "Re-running is cheap: run_workflow(resume_run_id=...) replays the cells that "
    "already completed and only re-spawns what died. A resume's launch reply "
    "carries cache_preview{replay, invalidate, never_completed, tokens_to_repay, "
    "invalidated[{node_id, reason}]} — READ IT BEFORE accepting the resume: an "
    "'identity_changed' entry is a node that ALREADY had a cell and will run "
    "again because YOU changed it, so confirm with the human that the change is "
    "intentional before re-paying tokens_to_repay; 'unknown' entries (pipeline, "
    "workflow, anything downstream of a miss) are nodes the preview does not "
    "recompute, never free replays. A 'paused' status means the "
    "run stopped RESUMABLY, not that the spec failed — it keeps its finished "
    "nodes. Spent 'token_budget' (the optional cap on what the whole run may "
    "spend, reported back as {total, spent, remaining}) is a human decision, "
    "never an agent one.\n"
    "The OPERATOR may have pre-authorized a token ceiling for this process: when "
    "one is in force the launch reply carries token_budget{total, source, "
    "operator_cap} — your own 'token_budget' is CLAMPED to it (on a resume too), "
    "so asking for more never raises it; only the human operator can.\n"
    "While a run is in flight you can always look: workflow_status reports live "
    "'progress' per node, workflow_list shows every run at once, and "
    "workflow_pause stops one resumably (nothing in flight is thrown away).\n"
    "READING A RUN (run-level decisions first): make run-level decisions (wait, "
    "resume, escalate) on workflow_status alone. workflow_audit is ON DEMAND for a "
    "leaf-level question only — one leaf's lifecycle or identity — and paginated "
    "with after_seq and a limit, never read whole. A local observation costs no "
    "provider call, but its JSON payload still lands in YOUR context: the "
    "containing supervisor/provider turn is metered in aggregate, while this "
    "payload is not separately attributed and is never charged to the workflow "
    "run — read less, not more. No fixed blind polling. A terminal notification "
    "is only an opportunistic queued hint: it DOES NOT wake or start a turn, is "
    "visible only if another agent-loop iteration/turn drains the queue; its "
    "callback can be unwired, it is skipped for a run you cancelled yourself, and delivery can fail "
    "silently. If this turn must observe a terminal boundary, "
    "workflow_status(wait=true) is the built-in blocking read, but its timeout is "
    "internal and fixed — not a caller-selected deliberate deadline. Otherwise "
    "recheck only when the execution environment can schedule a bounded wait; if "
    "it cannot, report no active watcher instead of pretending. Absence and "
    "silence are unknown, never idle. NO read — status, "
    "audit tail or silence — can't tell slow from wedged; a read only updates "
    "your last observed state. An audit gap does not prove silence either. "
    "Audit is metadata-only: observed metadata is NOT the leaf's current action, "
    "and raw content or reasoning is never in it. Identity has two levels: the "
    "logical target uses run_id, node_path, role and fan-out coordinates; an "
    "execution occurrence uses segment_id, attempt, turn and an ephemeral sub_id. "
    "A cache replay has no execution or sub_id, and identity never promises "
    "universal leaf-to-cache correlation.\n"
    "The status reply ends in an 'observation' block: 'source' says "
    "'local_registry' (read off the run state this process holds) or "
    "'durable_store' (rebuilt from the run's persisted line, possibly written "
    "by another process or before a restart) — the two primary read paths, "
    "both local and over mixed persisted data. 'provider_calls' is 'none' (the "
    "read is local), 'supervisor_context_tokens' is 'not_separately_attributed' "
    "— the payload lands in your context, metered only in the aggregate of the "
    "turn that contains it — and 'workflow_token_ledger_delta' is 0: nothing "
    "here is charged to the workflow run.\n"
    "STEERING A LIVE LEAF (workflow_steer — a WORKAROUND, not a watchtower; "
    "SUP-01): workflow_audit is how you discover a leaf's ephemeral sub_id for "
    "ONE live execution occurrence of a run that is running in THIS process; "
    "workflow_steer(run_id, sub_id, segment_id, attempt, turn, text) then "
    "accepts ONLY that exact observed occurrence; all coordinates must still "
    "match atomically at enqueue — a stale or ambiguous identity, a "
    "cache replay (no sub_id), a durable-only line or a run owned by another "
    "process is REJECTED, fail-closed. Queued acceptance is NOT read and NOT "
    "delivery: the text never preempts the provider turn in flight or a tool "
    "call, never mutates the leaf's FROZEN prompt, and reaches the leaf only "
    "BETWEEN loop iterations as a system reminder, if the leaf is still "
    "running. The operator budgets it: 1 external steer per leaf, 3 per run (a DURABLE ceiling across resume/restart), "
    "and 2 CUMULATIVE corrections per leaf — the same pool the schema-retry "
    "steering spends. Outcomes are audited: accepted (queued), read (delivered "
    "— SPENDS the slot), discarded (never landed — RESTORES the slot), "
    "rejected (orchestration refused; slot rolled back), exhausted (ceiling "
    "hit). Steering is a WORKAROUND under the SUP-01 supervision doctrine: "
    "record diagnosis and outcome like any adaptation, obey the per-key and "
    "GLOBAL no-progress brakes, and when the problem is STRUCTURAL (a bad "
    "spec or prompt) prefer workflow_cancel + a corrected re-run — steer only "
    "a SMALL CAUSAL correction of one live leaf.\n"
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
        "status 'degraded' means at least one node ended null or a fault exists: "
        "read 'faults' (and 'faults_total' on a resumed run) before trusting "
        "'outputs'; say which parts are missing instead of writing around holes. "
        "status 'failed' means every node nulled — re-author the spec, do not "
        "paper over it with a summary that pretends nothing went wrong. "
        "For the full supervision doctrine (the watch→diagnose→adapt→resume "
        "loop, the brakes, which faults are yours to fix vs the human's), load "
        "the workflow-authoring skill. "
        "A run still marked 'running' with 'stale' true is one whose process was "
        "lost — the agent may resume it under the supervision brakes; its finished "
        "cells replay. "
        "Every successful reply ends in an `observation` block saying where the "
        "facts came from. 'source' names the primary read path: 'local_registry' "
        "(the run state THIS process holds) or 'durable_store' (rebuilt from the "
        "run's persisted line — which may have been written by ANOTHER PROCESS or "
        "before a restart; both reads are local over mixed persisted data). "
        "'provider_calls' is 'none' (the read makes no provider call), and "
        "'supervisor_context_tokens' is 'not_separately_attributed' — the JSON you "
        "read back lands in YOUR context, metered only in the turn's aggregate, "
        "and is not charged to the workflow run; 'workflow_token_ledger_delta' "
        "is 0. "
        "The 'no workflow run' error carries no observation."
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
_STEER_SCHEMA = {
    "description": (
        "Steer a workflow run's LIVE EXECUTION OCCURRENCE: queue an instruction "
        "into one leaf sub-session of a run that is running in THIS process. "
        "Supply the observed segment_id, attempt and turn; any coordinate drift "
        "is rejected atomically before budget is spent. "
        "The text is QUEUED, never an interruption: it does not preempt the "
        "turn in flight and its DELIVERY is not guaranteed — the leaf sees it "
        "between loop iterations as a system reminder, if it is still running. "
        "Budgeted by the operator's steering limits: 1 external steer per "
        "leaf, 3 per run, durably across resume/restart, and 2 total corrections (external + internal) per "
        "leaf; a refused steer reports which ceiling was hit and the counters."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "The running workflow's run_id"},
            "sub_id": {
                "type": "string",
                "description": "The leaf sub-session to steer (its sub_id)",
            },
            "segment_id": {
                "type": "string",
                "description": "Observed segment_id of this exact live occurrence",
            },
            "attempt": {
                "type": "integer",
                "minimum": 0,
                "description": "Observed attempt coordinate of this exact occurrence",
            },
            "turn": {
                "type": "integer",
                "minimum": 0,
                "description": "Observed turn coordinate of this exact occurrence",
            },
            "text": {
                "type": "string",
                "description": "The instruction, non-empty, max 4000 chars",
            },
        },
        "required": ["run_id", "sub_id", "segment_id", "attempt", "turn", "text"],
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
        # A spec EXPLÍCITA do agente é encaminhada em qualquer shape (SUP-05):
        # um non-object (list/string/escalar) é falha de AUTORIA que o
        # validate_spec rejeita com o erro didático "the spec must be a
        # mapping" — e a proveniência abaixo registra a candidata. Recusar
        # aqui na porta esconderia o fault de quem aprende com ele. O que
        # continua recusado antes do serviço é só a ausência total de spec
        # num run fresco (nada para validar, nada para aprender).
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
            # This surface is the AGENT authoring (SUP-05): a spec validate_spec
            # rejects here is a high-confidence authoring fault and records a
            # durable candidate before the didactic error comes back. Operator
            # and test calls of WorkflowService.start do NOT set this — an
            # invalid spec nobody's agent sent is not the agent's lesson.
            # Proveniência (SUP-05): agency_authored só quando a spec veio
            # EXPLÍCITA nesta chamada. Um resume sem spec repete a spec
            # PERSISTIDA — autoria de outro momento, nunca da agência atual —
            # então a flag vai False e o serviço não atribui a spec herdada.
            agency_authored=spec is not None,
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

    def steer(self, args: dict[str, Any]) -> str:
        run_id = args.get("run_id")
        sub_id = args.get("sub_id")
        text = args.get("text")
        segment_id = args.get("segment_id")
        attempt = args.get("attempt")
        turn = args.get("turn")
        if not run_id or not sub_id:
            return tool_error("workflow_steer needs a 'run_id' and a 'sub_id'")
        if not isinstance(segment_id, str) or not segment_id:
            return tool_error("workflow_steer needs a non-empty 'segment_id'")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            return tool_error("workflow_steer needs a non-negative integer 'attempt'")
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
            return tool_error("workflow_steer needs a non-negative integer 'turn'")
        if not isinstance(text, str) or not text:
            return tool_error("workflow_steer needs a non-empty 'text' string")
        out = self._service.steer(
            str(run_id),
            str(sub_id),
            text,
            segment_id=segment_id,
            attempt=attempt,
            turn=turn,
        )
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
        "workflow_steer", "workflow", _STEER_SCHEMA, _intercepted, override=True, emoji="🎯"
    )
    registry.register(
        "workflow_templates", "workflow", _TEMPLATES_SCHEMA, _intercepted, override=True, emoji="📚"
    )
