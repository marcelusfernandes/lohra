"""Sonda ao vivo da janela de abort sob o backend Codex (issue #59).

``abort_check`` é consultado no TOPO do corpo do ``for`` de cada evento do
stream, ou seja, o intervalo entre duas chamadas É a latência do abort naquele
instante. Medindo os intervalos com e sem ``reasoning.summary`` obtém-se
exatamente o número que a issue pede — sem inventar instrumentação nova.

Roda também um SEGUNDO turno que replaya o reasoning item capturado (agora com
`summary` preenchido) para verificar ao vivo se o backend aceita o replay.

uso: python3 probe_summary.py <model> <auto|off|detailed|concise> [rotulo]
"""

import json
import os
import sys
import time

MODEL = sys.argv[1]
MODE = sys.argv[2]
LABEL = sys.argv[3] if len(sys.argv) > 3 else MODE

os.environ["LOHRA_PROFILE"] = "lohra-dogfood-w75"
os.environ["LOHRA_RESPONSES_REASONING_SUMMARY"] = MODE

PROMPT = (
    "Prove, step by step and from first principles, that the Ramsey number "
    "R(3,3) = 6: give the pigeonhole argument for the upper bound AND an "
    "explicit 2-colouring of K5 with no monochromatic triangle for the lower "
    "bound. Then write a 200-word note on why the pigeonhole principle is the "
    "engine behind Ramsey-type theorems."
)

from lohra.memory.paths import lohra_home  # noqa: E402
from lohra.providers.transports.responses import ResponsesTransport  # noqa: E402
from lohra.subscription.provider import build_subscription_client  # noqa: E402

import lohra  # noqa: E402

# Prova de qual código está rodando: `python3 caminho/script.py` põe o diretório
# do SCRIPT em sys.path[0], não o cwd — sem PYTHONPATH a sonda importaria o
# checkout instalado (foi o que aconteceu na primeira rodada).
print(f"[{LABEL}] lohra module = {lohra.__file__}", flush=True)

T = ResponsesTransport()
client = build_subscription_client(lohra_home())


def one_call(messages: list[dict], tag: str) -> tuple[dict, dict | None]:
    kwargs = T.build_kwargs(model=MODEL, messages=messages, effort="high")
    t0 = time.time()
    stamps: list[float] = []
    first_reasoning: list[float] = []
    first_text: list[float] = []

    def probe() -> bool:
        stamps.append(time.time() - t0)
        return False

    calls = {"reasoning": 0}

    def on_reasoning(chunk: str) -> None:
        # Contar as chamadas é o ÚNICO teste do dedup contra os eventos REAIS do
        # SDK: TIPOS-AUTO viu 14 deltas + 14 dones, então 14 chamadas = dedup ok,
        # 28 = `_summary_key` não bate nos objetos reais e a live view mostraria
        # cada pensamento duas vezes.
        calls["reasoning"] += 1
        if not first_reasoning:
            first_reasoning.append(time.time() - t0)

    first_text_idx: list[int] = []

    def on_text(chunk: str) -> None:
        if not first_text:
            first_text.append(time.time() - t0)
            # O evento que carrega o 1º texto já foi carimbado por probe().
            first_text_idx.append(len(stamps))

    error = None
    out = None
    try:
        out = client.stream(
            on_text=on_text, on_reasoning=on_reasoning, abort_check=probe, **kwargs
        )
        status = out.get("status") if isinstance(out, dict) else type(out).__name__
    except Exception as exc:  # noqa: BLE001 — a sonda REPORTA o 400, não morre nele
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
        status = "EXCEPTION"

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    # A MÉTRICA DA ISSUE: o maior silêncio ANTES do primeiro token de saída, ou
    # seja, dentro da fase de raciocínio — que é onde o abort não alcançava.
    cut = first_text_idx[0] if first_text_idx else len(stamps)
    pre = gaps[: max(cut - 1, 0)]
    report = {
        "max_gap_before_text_s": round(max(pre), 2) if pre else None,
        "mean_gap_before_text_s": round(sum(pre) / len(pre), 3) if pre else None,
        # Espera ESPERADA por um cancel que chega num instante aleatório da fase de
        # raciocínio: um silêncio longo é proporcionalmente mais provável de ser
        # aquele em que se cai (viés de comprimento), então a média simples
        # subestima o que o operador sente.
        "expected_wait_before_text_s": round(
            sum(g * g for g in pre) / (2 * sum(pre)), 2
        ) if pre and sum(pre) else None,
        "events_before_text": cut,
        "on_reasoning_calls": calls["reasoning"],
        "tag": tag,
        "reasoning_kwarg": kwargs.get("reasoning"),
        "replayed_reasoning_items": sum(
            1 for i in kwargs["input"] if isinstance(i, dict) and i.get("type") == "reasoning"
        ),
        "status": status,
        "error": error,
        "total_seconds": round(time.time() - t0, 2),
        "events": len(stamps),
        "first_event_s": round(stamps[0], 2) if stamps else None,
        "first_reasoning_s": round(first_reasoning[0], 2) if first_reasoning else None,
        "first_text_s": round(first_text[0], 2) if first_text else None,
        # A LATÊNCIA DO ABORT: o maior silêncio entre dois eventos consecutivos.
        "max_gap_s": round(max(gaps), 2) if gaps else None,
        "p95_gap_s": round(sorted(gaps)[min(int(len(gaps) * 0.95), len(gaps) - 1)], 3)
        if gaps else None,
        "median_gap_s": round(sorted(gaps)[len(gaps) // 2], 3) if gaps else None,
    }
    return report, out


msgs = [{"role": "user", "content": PROMPT}]
turn1, raw = one_call(msgs, "turn1")
print(f"[{LABEL}] " + json.dumps(turn1), flush=True)

# --- segundo turno: replay do reasoning item (agora com `summary` preenchido) ---
turn2 = {"tag": "turn2-replay", "skipped": "turn1 não produziu resposta"}
if isinstance(raw, dict):
    nr = T.normalize_response(raw)
    pdata = nr.provider_data or {}
    summary_lens = [
        len(i.get("summary") or []) for i in (pdata.get("reasoning_items") or [])
    ]
    msgs2 = [
        {"role": "user", "content": PROMPT},
        {"role": "assistant", "content": nr.content or "", "provider_data": dict(pdata)},
        {"role": "user", "content": "Now state the theorem in ONE sentence."},
    ]
    turn2, _ = one_call(msgs2, "turn2-replay")
    turn2["captured_summary_parts_per_item"] = summary_lens
print(f"[{LABEL}] " + json.dumps(turn2), flush=True)
print(f"[{LABEL}] DONE", flush=True)
