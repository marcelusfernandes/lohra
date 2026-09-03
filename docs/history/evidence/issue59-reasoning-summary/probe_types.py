"""Histograma de TIPOS de evento do stream de Responses, com o instante do
primeiro de cada tipo (issue #59).

A sonda de cadência mede o intervalo entre eventos mas não vê o que eles SÃO —
e a pergunta que decide o texto do documento é: o que o backend Codex entrega
durante o raciocínio quando NÃO se pede `summary`? Esta itera o stream cru.

uso: python3 probe_types.py <model> <auto|off|detailed|concise> [rotulo]
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

print(f"[{LABEL}] lohra module = {lohra.__file__}", flush=True)

T = ResponsesTransport()
kwargs = T.build_kwargs(
    model=MODEL, messages=[{"role": "user", "content": PROMPT}], effort="high"
)
client = build_subscription_client(lohra_home())

t0 = time.time()
counts: dict[str, int] = {}
first_at: dict[str, float] = {}
last_t = t0
worst = (0.0, "<início>", "<início>")
prev_type = "<início>"

stream = client._client.responses.create(stream=True, **kwargs)
try:
    for event in stream:
        now = time.time()
        etype = getattr(event, "type", None) or "<sem type>"
        gap = now - last_t
        if gap > worst[0]:
            worst = (gap, prev_type, etype)
        last_t, prev_type = now, etype
        counts[etype] = counts.get(etype, 0) + 1
        first_at.setdefault(etype, round(now - t0, 2))
        if etype == "response.completed":
            break
finally:
    close = getattr(stream, "close", None)
    if callable(close):
        close()

print(f"[{LABEL}] " + json.dumps({
    "label": LABEL,
    "reasoning_kwarg": kwargs.get("reasoning"),
    "total_seconds": round(time.time() - t0, 2),
    "counts": counts,
    "first_at_s": first_at,
    "worst_gap_s": round(worst[0], 2),
    "worst_gap_between": f"{worst[1]} -> {worst[2]}",
}, indent=2), flush=True)
