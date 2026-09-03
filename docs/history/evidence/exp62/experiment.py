"""Issue #62 — fan-out intra-nó sobre recurso compartilhado (experimento controlado).

Zero LLM: o "modelo" é um stub determinístico (``ScriptedLeafClient``) que emite
tool calls REAIS (``read_file``/``write_file``/``terminal``) pelo dispatch de
produção (``delegate.make_child_factory`` → ``registry.dispatch``), dentro do
``WorkflowService`` de main@0.0.23, com ``WorkflowPolicy`` construída à mão e um
``fs_allow`` apontando para um diretório do próprio experimento.

O stub NÃO roteiriza o resultado: ele faz um read-modify-write de verdade
(usa o conteúdo que o ``read_file`` devolveu), então uma perda de atualização
aqui é um fato do sistema, não do script.

Configurações
  A   parallel(3 branches): writerA + writerB (RMW via write_file) + reader
  B1  idem A, mas writers usam `terminal` com `printf ... >> shared.txt` (append)
  B2  idem A, mas writers usam `terminal` com `cat` + `printf ... > shared.txt` (RMW no shell)
  C   pipeline(items=[A,B], stages=[writer(schema_ref=artifact_manifest), reader])
  C2  validação: `schema_ref` numa branch de `parallel` (autorável? medido?)
  D   parallel(3): writers em ARQUIVOS DISTINTOS + reader (padrão recomendado)

Modos de interleaving
  jitter   sleeps aleatórios (0–8 ms) entre read e write → distribuição natural
  barrier  threading.Barrier(2) entre o read e o write dos dois writers →
           prova que a perda é ALCANÇÁVEL de forma determinística
           (a frequência 100% do modo barrier NUNCA é reportada como taxa)

Uso:
    LOHRA_HOME=<scratch>/lohra_home python3 experiment.py --reps 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

SCRATCH = Path(__file__).resolve().parent
os.environ.setdefault("LOHRA_HOME", str(SCRATCH / "lohra_home"))
os.environ.pop("LOHRA_PROFILE", None)
os.environ["LOHRA_AUDIT"] = "off"  # o ledger não é o instrumento deste experimento

from lohra.agent.delegate import make_child_factory  # noqa: E402
from lohra.providers import get_provider_profile  # noqa: E402
from lohra.state import SessionDB  # noqa: E402
from lohra.tools import fs as _fs  # noqa: E402,F401  (auto-registro)
from lohra.tools import terminal as _terminal  # noqa: E402,F401
from lohra.tools.registry import registry  # noqa: E402
from lohra.workflow.sandbox import WorkflowPolicy  # noqa: E402
from lohra.workflow.schema import ValidationError, validate_spec  # noqa: E402
from lohra.workflow.service import WorkflowService  # noqa: E402

LINE_A = "LINE-FROM-WRITER-A"
LINE_B = "LINE-FROM-WRITER-B"
_MARKER = re.compile(r"\[(WRITER-A|WRITER-B|READER)\]")
_PATH = re.compile(r"<PATH:([^>]+)>")


# --------------------------------------------------------------------------
# instrumento: log de observação (o arquivo final não é o instrumento)
# --------------------------------------------------------------------------
class Trace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.leaf_calls = 0

    def log(self, **row: Any) -> None:
        with self._lock:
            row["t"] = time.perf_counter()
            self.rows.append(row)

    def bump(self) -> None:
        with self._lock:
            self.leaf_calls += 1

    def of(self, leaf: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("leaf") == leaf]


# --------------------------------------------------------------------------
# o stub: um "modelo" que lê o prompt, usa o resultado da tool e dorme
# --------------------------------------------------------------------------
def _text(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _call(cid: str, name: str, args: dict) -> dict:
    return {
        "content": [{"type": "tool_use", "id": cid, "name": name, "input": args}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _tool_msgs(messages: list[dict]) -> list[dict]:
    """Os resultados de tool que o transport anthropic entrega ao provider.

    Não existe ``role: "tool"`` neste transport: um resultado chega como um bloco
    ``tool_result`` dentro de uma mensagem ``user`` (verificado empiricamente
    contra ``lohra.agent.loop`` antes de escrever o stub)."""
    blocks: list[dict] = []
    for m in messages:
        content = m.get("content")
        if m.get("role") == "user" and isinstance(content, list):
            blocks.extend(b for b in content if b.get("type") == "tool_result")
    return blocks


def _user_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            parts.append(m["content"])
    return "\n".join(parts)


def _payload(block: dict) -> dict:
    """O envelope JSON dentro de um bloco ``tool_result``."""
    try:
        return json.loads(block.get("content") or "{}")
    except (ValueError, TypeError):
        return {}


class ScriptedLeafClient:
    """Stub compartilhado por todos os leaves; estado vem das MENSAGENS."""

    def __init__(self, cfg: dict[str, Any], trace: Trace) -> None:
        self.cfg = cfg
        self.trace = trace

    # ---- provider surface -------------------------------------------------
    def create(self, **kwargs: Any) -> dict:
        messages = kwargs.get("messages") or []
        prompt = _user_text(messages)
        marker = _MARKER.search(prompt)
        role = marker.group(1) if marker else "UNKNOWN"
        paths = _PATH.findall(prompt)
        idx = len(_tool_msgs(messages))
        if idx == 0:
            self.trace.bump()
        self.trace.log(leaf=role, call=idx, phase="model")
        handler = getattr(self, f"_{self.cfg['kind']}_{role.replace('-', '_').lower()}", None)
        if handler is None:
            return _text("no-op")
        return handler(idx, _tool_msgs(messages), paths)

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs: Any) -> dict:
        return self.create(**kwargs)

    def close(self) -> None:
        return None

    # ---- sincronização ----------------------------------------------------
    def _pause(self, role: str) -> None:
        mode = self.cfg["mode"]
        if mode == "barrier" and "WRITER" in role:
            try:
                self.cfg["barrier"].wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        else:
            time.sleep(random.uniform(0, self.cfg.get("jitter", 0.008)))

    # ---- A / D: RMW puro com write_file -----------------------------------
    def _fs_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            self.trace.log(leaf=line, call=idx, phase="read.request", path=target)
            return _call("c0", "read_file", {"path": target})
        if idx == 1:
            got = _payload(results[-1])
            prev = got.get("data") or ""
            self.trace.log(leaf=line, call=idx, phase="read.done", got=prev)
            self._pause(line)
            content = f"{prev}{line}\n"
            self.trace.log(leaf=line, call=idx, phase="write.request", wrote=content)
            return _call("c1", "write_file", {"path": target, "content": content})
        wrote = _payload(results[-1])
        return _text(json.dumps({"claim": f"appended {line}", "write": wrote}))

    def _fs_writer_a(self, idx, results, paths):
        return self._fs_writer(idx, results, paths, LINE_A)

    def _fs_writer_b(self, idx, results, paths):
        return self._fs_writer(idx, results, paths, LINE_B)

    def _fs_reader(self, idx, results, paths):
        target = paths[0]
        if idx == 0:
            time.sleep(self.cfg.get("read_delay", 0.004))
            return _call("r0", "read_file", {"path": target})
        got = _payload(results[-1])
        return _text(json.dumps({"saw": got.get("data", None), "error": got.get("error")}))

    _a_writer_a = _fs_writer_a
    _a_writer_b = _fs_writer_b
    _a_reader = _fs_reader

    # ---- D: arquivos distintos -------------------------------------------
    def _d_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            self._pause(line)
            self.trace.log(leaf=line, call=idx, phase="write.request", path=target)
            return _call("d0", "write_file", {"path": target, "content": f"{line}\n"})
        return _text(json.dumps({"claim": f"wrote {line}", "path": target}))

    def _d_writer_a(self, idx, results, paths):
        return self._d_writer(idx, results, paths, LINE_A)

    def _d_writer_b(self, idx, results, paths):
        return self._d_writer(idx, results, paths, LINE_B)

    def _d_reader(self, idx, results, paths):
        # lê os dois arquivos, um por vez
        if idx == 0:
            time.sleep(self.cfg.get("read_delay", 0.004))
            return _call("dr0", "read_file", {"path": paths[0]})
        if idx == 1:
            self.cfg.setdefault("_seen", {})
            return _call("dr1", "read_file", {"path": paths[1]})
        first, second = _payload(results[-2]), _payload(results[-1])
        return _text(json.dumps({"a": first.get("data"), "a_err": first.get("error"),
                                 "b": second.get("data"), "b_err": second.get("error")}))

    # ---- B1: append via shell (O_APPEND) ----------------------------------
    def _b1_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            self._pause(line)
            cmd = f"printf '%s\\n' {line} >> {target}"
            self.trace.log(leaf=line, call=idx, phase="write.request", cmd=cmd)
            return _call("s0", "terminal", {"command": cmd})
        res = _payload(results[-1])
        return _text(json.dumps({"claim": f"appended {line}", "shell": res}))

    def _b1_writer_a(self, idx, results, paths):
        return self._b1_writer(idx, results, paths, LINE_A)

    def _b1_writer_b(self, idx, results, paths):
        return self._b1_writer(idx, results, paths, LINE_B)

    _b1_reader = _fs_reader

    # ---- B2: RMW via shell (cat + truncating redirect) --------------------
    def _b2_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            return _call("s0", "terminal", {"command": f"cat {target} 2>/dev/null || true"})
        if idx == 1:
            res = _payload(results[-1])
            prev = res.get("stdout") or ""
            self.trace.log(leaf=line, call=idx, phase="read.done", got=prev)
            self._pause(line)
            content = f"{prev}{line}\n"
            payload = content.replace("'", "'\\''")
            cmd = f"printf '%s' '{payload}' > {target}"
            self.trace.log(leaf=line, call=idx, phase="write.request", wrote=content)
            return _call("s1", "terminal", {"command": cmd})
        return _text(json.dumps({"claim": f"appended {line}"}))

    def _b2_writer_a(self, idx, results, paths):
        return self._b2_writer(idx, results, paths, LINE_A)

    def _b2_writer_b(self, idx, results, paths):
        return self._b2_writer(idx, results, paths, LINE_B)

    _b2_reader = _fs_reader

    # ---- C: RMW + manifesto de artefato -----------------------------------
    def _c_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            return _call("c0", "read_file", {"path": target})
        if idx == 1:
            got = _payload(results[-1])
            prev = got.get("data") or ""
            self._pause(line)
            content = f"{prev}{line}\n"
            self.cfg.setdefault("_claims", {})[line] = content
            self.trace.log(leaf=line, call=idx, phase="write.request", wrote=content)
            return _call("c1", "write_file", {"path": target, "content": content})
        content = self.cfg.get("_claims", {}).get(line, "")
        raw = content.encode("utf-8")
        return _text(json.dumps({
            "path": target,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }))

    def _c_writer_a(self, idx, results, paths):
        return self._c_writer(idx, results, paths, LINE_A)

    def _c_writer_b(self, idx, results, paths):
        return self._c_writer(idx, results, paths, LINE_B)

    _c_reader = _fs_reader

    # ---- C3: manifesto MÍNIMO (só `path`) — o schema só exige `path` ------
    def _c3_writer(self, idx, results, paths, line):
        target = paths[0]
        if idx == 0:
            return _call("c0", "read_file", {"path": target})
        if idx == 1:
            got = _payload(results[-1])
            prev = got.get("data") or ""
            self.trace.log(leaf=line, call=idx, phase="read.done", got=prev)
            self._pause(line)
            content = f"{prev}{line}\n"
            self.trace.log(leaf=line, call=idx, phase="write.request", wrote=content)
            return _call("c1", "write_file", {"path": target, "content": content})
        return _text(json.dumps({"path": target}))

    def _c3_writer_a(self, idx, results, paths):
        return self._c3_writer(idx, results, paths, LINE_A)

    def _c3_writer_b(self, idx, results, paths):
        return self._c3_writer(idx, results, paths, LINE_B)

    _c3_reader = _fs_reader


# --------------------------------------------------------------------------
# specs
# --------------------------------------------------------------------------
def _parallel_spec(name: str, shared: str, *, distinct: bool = False) -> dict:
    if distinct:
        wa = f"[WRITER-A] write your own file <PATH:{shared}/a.txt>"
        wb = f"[WRITER-B] write your own file <PATH:{shared}/b.txt>"
        rd = f"[READER] read <PATH:{shared}/a.txt> and <PATH:{shared}/b.txt>"
    else:
        wa = f"[WRITER-A] append your line to <PATH:{shared}/shared.txt>"
        wb = f"[WRITER-B] append your line to <PATH:{shared}/shared.txt>"
        rd = f"[READER] read <PATH:{shared}/shared.txt>"
    return {
        "meta": {"name": name, "version": 1},
        "nodes": [{"id": "fanout", "type": "parallel", "branches": [wa, wb, rd]}],
    }


def _pipeline_spec(shared: str) -> dict:
    return {
        "meta": {"name": "exp62-c", "version": 1},
        "nodes": [
            {
                "id": "pipe",
                "type": "pipeline",
                "items": ["A", "B"],
                "stages": [
                    {
                        "prompt": (
                            "[WRITER-${item}] append your line to "
                            f"<PATH:{shared}/shared.txt> and return the manifest"
                        ),
                        "schema_ref": "artifact_manifest",
                    },
                    {"prompt": f"[READER] read <PATH:{shared}/shared.txt> "
                              "after ${stage.result}"},
                ],
            }
        ],
    }


# --------------------------------------------------------------------------
# classificador de desfecho (definido ANTES de rodar)
# --------------------------------------------------------------------------
def classify(final: str) -> str:
    lines = [line for line in final.splitlines() if line.strip()]
    has_a, has_b = LINE_A in lines, LINE_B in lines
    if not lines:
        return "empty"
    if has_a and has_b and len(lines) == 2:
        return "both_AB" if lines.index(LINE_A) < lines.index(LINE_B) else "both_BA"
    if has_a and not has_b and len(lines) == 1:
        return "lost_update_B"  # o B perdeu: só a linha do A sobrou
    if has_b and not has_a and len(lines) == 1:
        return "lost_update_A"
    if not has_a and not has_b:
        return "torn"
    return f"other({len(lines)}L,a={has_a},b={has_b})"


def overlapped(trace: Trace) -> bool:
    """Os dois writers realmente se sobrepuseram? (validade da repetição)

    Sobreposição = o read de um writer aconteceu ANTES da escrita do outro.
    Se nunca acontece, o pool serializou e a repetição não mediu nada."""
    def stamp(leaf: str, phase: str) -> float | None:
        rows = [r for r in trace.rows if r.get("leaf") == leaf and r.get("phase") == phase]
        return rows[0]["t"] if rows else None

    a_read, a_write = stamp(LINE_A, "read.done"), stamp(LINE_A, "write.request")
    b_read, b_write = stamp(LINE_B, "read.done"), stamp(LINE_B, "write.request")
    if None in (a_write, b_write):
        return False
    if a_read is None or b_read is None:  # B1/D: sem fase de read
        return True
    return (a_read < b_write and b_read < a_write)


# --------------------------------------------------------------------------
# uma repetição
# --------------------------------------------------------------------------
def run_rep(kind: str, mode: str, rep: int, root: Path, *, resume: bool = False) -> dict:
    workdir = root / f"{kind}-{mode}-{rep:03d}"
    if workdir.exists():
        shutil.rmtree(workdir)
    shared = workdir / "shared"
    home = workdir / "home"
    shared.mkdir(parents=True)
    home.mkdir(parents=True)
    (shared / "shared.txt").write_text("", encoding="utf-8")

    trace = Trace()
    cfg: dict[str, Any] = {
        "kind": kind if kind in {"b1", "b2", "c", "c3", "d"} else "a",
        "mode": mode,
        "barrier": threading.Barrier(2),
        "jitter": 0.008,
        "read_delay": 0.004,
    }
    client = ScriptedLeafClient(cfg, trace)
    factory = make_child_factory(
        model="stub-model",
        provider=get_provider_profile("anthropic"),
        client=client,
        tool_definitions=tuple(registry.get_definitions()),
    )
    policy = WorkflowPolicy(
        fs_allow=(str(shared),),
        allow_terminal=kind in {"b1", "b2"},
    )
    db = SessionDB(str(workdir / "state.db"))
    svc = WorkflowService(
        base_child_factory=factory, db=db, home=home, policy=policy, run_concurrency=4
    )
    out: dict[str, Any] = {"kind": kind, "mode": mode, "rep": rep}
    try:
        if kind in {"c", "c3"}:
            spec = _pipeline_spec(str(shared))
            node_id = "pipe"
        else:
            spec = _parallel_spec(f"exp62-{kind}", str(shared), distinct=(kind == "d"))
            node_id = "fanout"
        started = svc.start(spec, {})
        if "error" in started:
            out["error"] = started["error"]
            return out
        run_id = started["run_id"]
        final = svc.status(run_id, wait=True, timeout=120)
        out["status"] = final.get("status")
        out["faults"] = final.get("faults", [])
        out["outputs"] = final.get("outputs", {})
        out["overlapped"] = overlapped(trace)
        out["leaf_spawns"] = trace.leaf_calls
        if kind == "d":
            out["file_a"] = (shared / "a.txt").read_text(encoding="utf-8") if (shared / "a.txt").exists() else None
            out["file_b"] = (shared / "b.txt").read_text(encoding="utf-8") if (shared / "b.txt").exists() else None
            out["outcome"] = (
                "both_files_intact"
                if out["file_a"] == f"{LINE_A}\n" and out["file_b"] == f"{LINE_B}\n"
                else "damaged"
            )
        else:
            content = (shared / "shared.txt").read_text(encoding="utf-8")
            out["final_content"] = content
            out["final_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            out["outcome"] = classify(content)
        # o que o reader viu
        out["reader_saw"] = _reader_view(out["outputs"], kind)
        # células do cache
        out["cells"] = _cells(db, run_id, node_id)
        # resume: as células replayam? o que elas afirmam?
        if resume:
            trace.leaf_calls = 0
            again = svc.start(spec, {}, resume_run_id=run_id)
            if "error" not in again:
                rerun = svc.status(again["run_id"], wait=True, timeout=120)
                out["resume_status"] = rerun.get("status")
                out["resume_spawns"] = trace.leaf_calls
                out["resume_outputs"] = rerun.get("outputs", {})
                out["resume_faults"] = rerun.get("faults", [])
                out["resume_content"] = (shared / "shared.txt").read_text(encoding="utf-8") \
                    if (shared / "shared.txt").exists() else None
    finally:
        svc.shutdown()
        db.close()
    return out


def _reader_view(outputs: dict, kind: str) -> Any:
    node = outputs.get("pipe") if kind in {"c", "c3"} else outputs.get("fanout")
    if isinstance(node, list):
        for entry in node:
            if isinstance(entry, str) and '"saw"' in entry:
                try:
                    return json.loads(entry)
                except ValueError:
                    return entry
            if isinstance(entry, dict) and "saw" in entry:
                return entry
        return node
    return node


def _cells(db, run_id: str, node_id: str) -> list[dict[str, Any]]:
    """TODAS as células do run, lidas direto da tabela.

    Um pipeline guarda cada (item, stage) sob ``<node>#<item>#<stage>``, então
    perguntar pelo id do nó devolveria lista vazia (foi o 1º erro deste script)."""
    cur = db._connection.execute(  # noqa: SLF001 - leitura de instrumento
        "SELECT content_hash, node_id, output_json, artifact_verification, artifact_json "
        "FROM workflow_node_cache WHERE run_id = ? ORDER BY node_id",
        (run_id,),
    )
    return [
        {
            "hash": r[0][:12],
            "node_id": r[1],
            "output": r[2],
            "artifact_verification": r[3],
            "artifact_json": r[4],
        }
        for r in cur.fetchall()
    ]


# --------------------------------------------------------------------------
# C2 — schema_ref numa branch de parallel: autorável?
# --------------------------------------------------------------------------
def probe_c2() -> dict[str, Any]:
    spec = {
        "meta": {"name": "exp62-c2", "version": 1},
        "nodes": [{
            "id": "fanout",
            "type": "parallel",
            "branches": [
                {"prompt": "[WRITER-A] w", "schema_ref": "artifact_manifest"},
                {"prompt": "[WRITER-B] w", "schema_ref": "artifact_manifest"},
            ],
        }],
    }
    parsed = validate_spec(spec)
    if isinstance(parsed, ValidationError):
        return {"accepted": False, "message": parsed.message}
    return {"accepted": True, "branches": parsed.nodes[0].fields.get("branches")}


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--configs", default="a,b1,b2,c,c3,d")
    ap.add_argument("--modes", default="jitter,barrier")
    ap.add_argument("--out", default=str(SCRATCH / "results"))
    ap.add_argument("--resume-all", action="store_true")
    args = ap.parse_args()

    root = SCRATCH / "work"
    root.mkdir(parents=True, exist_ok=True)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    (outdir / "c2_probe.json").write_text(
        json.dumps(probe_c2(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("C2 probe:", json.dumps(probe_c2(), ensure_ascii=False)[:300])

    for kind in args.configs.split(","):
        for mode in args.modes.split(","):
            if kind in {"b1", "d"} and mode == "barrier":
                pass  # ainda vale: prova que mesmo sincronizados não se corrompem
            path = outdir / f"{kind}-{mode}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for rep in range(args.reps):
                    row = run_rep(kind, mode, rep, root,
                                  resume=(args.resume_all or rep == 0))
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    handle.flush()
                    print(f"{kind}/{mode} rep{rep:03d}: {row.get('outcome')} "
                          f"status={row.get('status')} overlap={row.get('overlapped')} "
                          f"faults={len(row.get('faults', []))}", flush=True)
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
