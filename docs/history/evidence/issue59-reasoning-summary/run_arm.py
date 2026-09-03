"""Roda UM braço da medição do #59 pela receita do briefing e cronometra.

O envelope --json só é impresso DEPOIS de ``workflow_service.shutdown()``
(cli.py:794 vs :813), então o wall-clock do processo já inclui a quiescência:
turno do agente + cancel + join. Comparar o mesmo prompt entre os dois braços
isola o que mudou. Depois do turno, puxa o ledger de auditoria do run para os
timestamps reais de ``leaf.started`` → terminal.

uso: python3 run_arm.py <before|after> <rotulo>
"""

import json
import os
import pathlib
import subprocess
import sys
import time

ARM = sys.argv[1]
LABEL = sys.argv[2]

WORKTREE = "/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/reasoning-summary/backend"
MAIN = "/Users/marcelusfernandes/Desktop/playground-ai/lohra/backend"
OUT = pathlib.Path(
    "/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/"
    "5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/arms"
)
OUT.mkdir(parents=True, exist_ok=True)

TASK = (
    "Call run_workflow with a spec that has ONE agent node with id 'think', "
    "provider 'openai-codex', model 'gpt-5.6-sol', effort 'high', and this "
    "prompt: \"Prove step by step, from first principles, that the Ramsey "
    "number R(3,3) = 6 (pigeonhole upper bound AND an explicit 2-colouring of "
    "K5 with no monochromatic triangle), then write a 1500-word essay on why "
    "the pigeonhole principle is the engine behind Ramsey-type theorems.\" "
    "Reply IMMEDIATELY with just the run_id; do NOT poll workflow_status."
)

cwd = WORKTREE if ARM == "after" else MAIN
env = dict(os.environ)
env["LOHRA_AUDIT"] = "1"
env["LOHRA_PROFILE"] = "lohra-dogfood-w75"
env["LOHRA_LIVEVIEW"] = "plain"
env.pop("LOHRA_RESPONSES_REASONING_SUMMARY", None)

BOOT = "from lohra.cli import main; import sys; sys.exit(main(sys.argv[1:]))"

# Prova de qual código este braço roda (o `-c` põe o cwd em sys.path[0]).
which = subprocess.run(
    [sys.executable, "-c", "import lohra; print(lohra.__file__)"],
    cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
).stdout.strip()

t0 = time.time()
proc = subprocess.run(
    [sys.executable, "-c", BOOT, "chat", "--json", TASK],
    cwd=cwd, env=env, capture_output=True, text=True, timeout=1800,
)
elapsed = time.time() - t0

(OUT / f"{LABEL}.stdout.json").write_text(proc.stdout)
(OUT / f"{LABEL}.stderr.txt").write_text(proc.stderr)

run_id = None
tool_names: list[str] = []
final = None
workflows = None
try:
    envelope = json.loads(proc.stdout)
    final = (envelope.get("output") or "")[:300]
    tool_names = [c.get("name") for c in envelope.get("tool_calls") or []]
    workflows = envelope.get("workflows")
    for wf in workflows or []:
        run_id = wf.get("run_id") or run_id
except Exception as exc:  # noqa: BLE001
    final = f"<stdout não é JSON: {exc}>"

audit_head = None
if run_id:
    audit = subprocess.run(
        [sys.executable, "-c", BOOT, "workflow", "audit", run_id],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )
    (OUT / f"{LABEL}.audit.txt").write_text(audit.stdout + "\n--- stderr ---\n" + audit.stderr)
    audit_head = [ln for ln in audit.stdout.splitlines() if "leaf" in ln or "run." in ln][:12]

print(json.dumps({
    "label": LABEL,
    "arm": ARM,
    "cwd": cwd,
    "lohra_module": which,
    "returncode": proc.returncode,
    "wall_clock_s": round(elapsed, 2),
    "run_id": run_id,
    "tool_calls": tool_names,
    "workflows": workflows,
    "final_response_head": final,
    "audit_leaf_lines": audit_head,
}, indent=2), flush=True)
