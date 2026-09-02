# Lohra dogfood battery A

Date: 2026-09-02  
Profile: lohra-dogfood-w75

## Summary

| Test | Result |
| --- | --- |
| T1 — leaf sandbox denies terminal by default | FAIL |
| T2 — disconnected DAG lint | PASS |
| T3 — malformed depends_on is rejected | PASS |

Every judged lohra chat command exited 0 and its envelope had error: null. T1/deny had one provider-overload attempt before the single authorized retry; that first attempt is recorded separately below.

## T1 — FAIL

Overall result: FAIL. The default-deny run did not execute the command and its leaf output did not contain 42, but it failed the required diagnostic contract: there was no sandbox-denial text and no allow_terminal or LOHRA_LEAF_ALLOW_TERMINAL remedy. The opt-in run passed.

### Default deny — judged retry

- Raw envelope: A-T1-1.json
- Session id: 1122787691654440b18c0071d5bffbec
- Run id: f6516b21be374b7a9d3e7b96428bdd86
- CLI exit: 0
- Envelope error: null
- Wall-clock: 28.03 s
- Cost:

    {"usd": 0.382492, "gross_usd": 0.54838, "saved_usd": 0.165888, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}

Exact workflow-status evidence:

> "status": "complete"

> "faults": []

> "outputs": {"shell": "No terminal tool is available."}

Exact outer response:

> Run status: complete
>
> Faults:
> (none)
>
> Leaf output:
> No terminal tool is available.

Expectation checks:

- No 42 in the leaf output: satisfied.
- Text like "'terminal' tool is disabled for workflow leaves (sandbox denied)": not present.
- Remedy allow_terminal or LOHRA_LEAF_ALLOW_TERMINAL: not present.

Surprise: the run reported no fault at all. The leaf returned "No terminal tool is available." instead of the requested exact tool result/error, making the deny indistinguishable from a missing tool and omitting the operator remedy.

### Default deny — transient first attempt

- Preserved raw envelope: A-T1-1-attempt1.json
- Session id: a018b1a5e55c446b8ab28d6e61cca905
- Run id: dc4d84cd07c74f3c9014dfe175ca78a4
- CLI exit: 1
- Wall-clock: 24.97 s
- Cost:

    {"usd": 0.080716, "gross_usd": 0.246604, "saved_usd": 0.165888, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}

Exact envelope error:

> Our servers are currently overloaded. Please try again later.

Exact workflow envelope state:

> {"run_id": "dc4d84cd07c74f3c9014dfe175ca78a4", "status": "running", "cancelled_on_exit": true}

The authorized retry was started after a 20-second wait. Surprise: stderr had shown the leaf reaching 1/1 nodes, but the outer provider overload left the envelope at status running with cancelled_on_exit: true; the later durable list reports the run as cancelled.

### Terminal opt-in — PASS

- Raw envelope: A-T1-2.json
- Session id: 57683c9752a9439ba11d76d81fc66592
- Run id: f0db0a225d4b4de4a7a603639d9ab58d
- CLI exit: 0
- Envelope error: null
- Wall-clock: 41.69 s
- Cost:

    {"usd": 0.354646, "gross_usd": 0.699324, "saved_usd": 0.344678, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}

Exact workflow-status evidence:

> "status": "complete"

> "faults": []

> "outputs": {"shell": "{\"ok\": true, \"stdout\": \"42\\n\", \"stderr\": \"\", \"exit_code\": 0}"}

Exact leaf output:

> {"ok": true, "stdout": "42\n", "stderr": "", "exit_code": 0}

The leaf output contains 42. No unexpected fault or agent deviation was observed in this opt-in run.

### Requested workflow list lines

Exact lines from LOHRA_PROFILE=lohra-dogfood-w75 lohra workflow list:

> f0db0a22  complete  1/1 nodes  1047 tok  shell

> f6516b21  complete  1/1 nodes  466 tok  shell

For completeness, the transient attempt also appears as:

> dc4d84cd  cancelled  1/1 nodes  458 tok  shell-tool-probe

The workflow-list command exited 0 in 0.55 s and has no chat-envelope cost field.

## T2 — PASS

- Raw envelope: A-T2-1.json
- Session id: 296c26928e7648ff9ab086822517d460
- Run id: 78358734acbf4e6181694207a9d263da
- CLI exit: 0
- Envelope error: null
- Wall-clock: 37.28 s
- Cost:

    {"usd": 0.171307, "gross_usd": 0.554232, "saved_usd": 0.382925, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}

Exact run_workflow result:

> {"ok": true, "run_id": "78358734acbf4e6181694207a9d263da", "status": "started", "warnings": [{"rule": "disconnected_dag", "message": "2 nodes share no 'depends_on' or ${ref} anywhere in this spec — they still run ONE AT A TIME, in a queue, just with no relation between them. If they should run together, make them branches of a 'parallel' node; if one needs another's output (or just needs to run after it), add 'depends_on' or a ${ref}. See skill_view('workflow-authoring') for worked examples.", "node_id": null}]}

Exact final-status evidence:

> "status": "complete"

> "faults": []

> "outputs": {"left": "LEFT", "right": "RIGHT"}

The warning rule is exactly disconnected_dag, its message names both parallel and depends_on, and the run completed. No unexpected fault or agent deviation was observed.

Requested durable-list line:

> 78358734  complete  2/2 nodes  722 tok  disconnected-validator-test

## T3 — PASS

- Raw envelope: A-T3-1.json
- Session id: b993eebccd9145649eecc058442ec079
- Run id: none
- CLI exit: 0
- Envelope error: null
- Wall-clock: 27.65 s
- Cost:

    {"usd": 0.14696, "gross_usd": 0.395792, "saved_usd": 0.248832, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}

Exact submitted node fragment from the run_workflow arguments:

> {"id":"b","type":"agent","depends_on":"a","prompt":"B"}

Exact run_workflow result:

> {"error": "invalid workflow spec: [depends_on_type] b .depends_on: 'depends_on' must be a list of node id strings, not str\n    e.g. depends_on: [\"scan\"]"}

The result contains rule depends_on_type, the required type message, and the example depends_on: ["scan"]. The tool result has no run_id, the envelope has no workflow_status call, and workflows is null; no run was started. No retry with a corrected spec occurred. No unexpected fault or agent deviation was observed.

## Raw artifacts

- A-T1-1.json — final default-deny retry
- A-T1-1-attempt1.json — preserved transient-overload attempt
- A-T1-2.json — terminal opt-in
- A-T2-1.json — disconnected DAG
- A-T3-1.json — malformed depends_on
- A-workflow-list.txt — durable workflow listing
