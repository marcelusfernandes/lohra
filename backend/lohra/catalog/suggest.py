"""Sugestão de tier map a partir do catálogo REAL — para o operador confirmar.

A dor que isto fecha: "a Lohra deveria saber o que tem disponível para escolher
modelos sem adivinhar". O tier map (``workflow_tiers.json``) é a ponte entre o
catálogo e o modo automático — mas ninguém o escreve à mão. Esta heurística
propõe um a partir do que está ALCANÇÁVEL agora (entradas ``live``/``config``
do catálogo), e é deliberadamente uma PROPOSTA: nomes de modelo não são
confiáveis o bastante entre providers para um write silencioso (anthropic lista
o flagship primeiro; gemini lista o flash primeiro), então o comando apresenta
e o humano confirma.

Sinais usados, na ordem de confiança:
1. ``default_aux_model`` do profile do provider — o sinal mais forte de "small"
   quando existe (só alguns profiles o declaram).
2. Padrões de nome (``flash``/``mini``/``haiku``/... = small; ``opus``/``pro``/
   ``large``/... = big) sobre os modelos que o catálogo confirmou existirem.
3. ``fallback_models`` do profile como desempate de ordenação.
"""

from __future__ import annotations

import re

from lohra.catalog.catalog import Catalog

# Padrões sobre o ID do modelo (case-insensitive). Deliberadamente curtos: um
# falso-positivo vira só uma sugestão pior, que o humano corrige na confirmação.
_SMALL = re.compile(r"(?<![a-z])mini\b|flash|lite|(?<![a-z])air\b|nano|haiku|small|tiny|turbo|[0-9](\.[0-9])?b\b|:free", re.I)
_MEDIUM = re.compile(r"sonnet", re.I)
_BIG = re.compile(r"opus|pro\b|large|ultra|max\b|k3\b|grok-4(\.\d+)?$|-5\b", re.I)

# Famílias NÃO-conversacionais que um /models real lista junto (dall-e, whisper,
# embeddings...): jamais podem virar tier — um `tier: medium` roteando gasto
# para um modelo de imagem foi o repro do review (2026-08-29).
_NON_CHAT = re.compile(
    r"embed|whisper|tts|dall-e|moderation|audio|realtime|transcribe|image|"
    r"clip\b|rerank|search-", re.I
)

_USABLE_SOURCES = ("live", "config")


def _classify(model: str) -> str:
    if _SMALL.search(model):
        return "small"
    if _MEDIUM.search(model):
        return "medium"
    if _BIG.search(model):
        return "big"
    return "medium"


def suggest_tiers(catalog: Catalog) -> dict[str, dict[str, str]]:
    """{tier: {"model", "provider"}} sugerido do que está alcançável, ou {}.

    Nunca levanta; nunca usa entradas ``skipped``/``error``. O resultado é
    material de apresentação — quem escreve o arquivo é o comando, após
    confirmação (ou ``--yes``).
    """
    from lohra.providers import get_provider_profile

    picks: dict[str, dict[str, str]] = {}
    candidates: list[tuple[str, str, str]] = []  # (classe, provider, model)

    for entry in catalog.entries:
        if entry.source not in _USABLE_SOURCES or not entry.models:
            continue
        try:
            profile = get_provider_profile(entry.provider)
        except Exception:  # noqa: BLE001 — openai-codex não está no registry
            profile = None
        aux = getattr(profile, "default_aux_model", None)
        if aux and aux in entry.models and "small" not in picks:
            picks["small"] = {"model": aux, "provider": entry.provider}
        for model in entry.models:
            if _NON_CHAT.search(model):
                continue
            candidates.append((_classify(model), entry.provider, model))

    for wanted in ("big", "small", "medium"):
        if wanted in picks:
            continue
        pool = candidates
        if wanted == "medium" and "big" in picks:
            # coerência primeiro: um medium do MESMO provider do big lê melhor
            # que um modelo aleatório de outro provider (dogfood 2026-08-28).
            same = [c for c in candidates if c[1] == picks["big"]["provider"]]
            pool = same + [c for c in candidates if c[1] != picks["big"]["provider"]]
        for klass, provider, model in pool:
            if klass != wanted:
                continue
            if any(p["model"] == model and p["provider"] == provider for p in picks.values()):
                continue
            picks[wanted] = {"model": model, "provider": provider}
            break

    # medium ausente com big presente: usa o segundo modelo não-small do mesmo
    # provider do big (ordem do catálogo), senão fica sem — apresentação decide.
    if "medium" not in picks and "big" in picks:
        big = picks["big"]
        for klass, provider, model in candidates:
            if provider != big["provider"] or model == big["model"] or klass == "small":
                continue
            if any(p["model"] == model and p["provider"] == provider for p in picks.values()):
                continue
            picks["medium"] = {"model": model, "provider": provider}
            break

    return picks
