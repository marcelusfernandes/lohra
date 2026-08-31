"""Cache de janelas de contexto por modelo (``<home>/model_windows.json``).

Duas velocidades muito diferentes se encontram aqui. O catálogo fala com as APIs
dos providers raramente e sob comando explícito (``lohra models``), e algumas
dessas APIs publicam a janela real de cada modelo — a OpenRouter publica, e é
justamente a rota onde nenhum default estático pode estar certo (centenas de
modelos, de 8k a 1M, sob um perfil só). O preflight de compactação, do outro
lado, roda a CADA iteração do loop e não pode tocar a rede.

Este módulo é a ponte: o catálogo grava o que viu, o loop lê. Regras que valem
mais que o cache em si:

* **Nunca levanta.** Json corrompido, home read-only, disco cheio, arquivo
  gigante — tudo degrada para "não sei", e quem chama cai no piso do perfil.
  Uma exceção daqui mataria um turno para *economizar* um turno.
* **Nunca faz rede.** É só leitura/escrita de um arquivo local.
* **Ausência ≠ zero.** Um provider que não publica a janela simplesmente não
  aparece; um id sem entrada devolve ``None``, não um número inventado.

Depende só da stdlib + ``lohra.memory.paths`` + ``lohra.safeio``: o Agent o
importa preguiçosamente e não pode arrastar o pacote ``catalog`` inteiro (com
httpx, tiers e a tool) para dentro do caminho quente.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

from pathlib import Path
from typing import Any, Mapping

from lohra import safeio
from lohra.memory.paths import lohra_home

logger = logging.getLogger(__name__)

FILENAME = "model_windows.json"

# Teto por provider para o arquivo não crescer sem limite ao longo de meses de
# `lohra models`. Ordem de inserção decide quem fica (o read mais recente vem
# primeiro no merge), então o corte é determinístico, não arbitrário.
MAX_MODELS_PER_PROVIDER = 2_000

# O arquivo é escrito por nós, mas é lido do disco do usuário: bounded como todo
# read do repo. O teto é dimensionado ACIMA do pior caso do cap de escrita —
# muitos providers × MAX_MODELS_PER_PROVIDER × ~60B por entrada — para que o
# limite REAL seja o cap por-provider, não este bound: cruzá-lo faria uma
# leitura truncada zerar o cache INTEIRO (todo provider cai no piso), um
# precipício silencioso em vez de uma perda proporcional (review adversarial #38).
MAX_FILE_BYTES = 8_000_000

# path resolvido -> (assinatura do arquivo, dados). O loop chama ``lookup`` a
# cada decisão de compactação; sem memo isso seria um parse de json por
# iteração. A assinatura (mtime_ns + tamanho) invalida sozinha quando o próprio
# processo — ou outro — reescreve o arquivo, então não há estado a resetar entre
# sessões. A chave é o PATH: dois profiles (ou dois tmp_path de teste) nunca
# compartilham resposta.
_MEMO: dict[Path, tuple[tuple[int, int], dict[str, dict[str, int]]]] = {}


def windows_path(home: Path | str | None = None) -> Path:
    """O arquivo do cache no home dado, ou no home do profile ativo."""
    root = Path(home) if home is not None else lohra_home()
    return root / FILENAME


def clear_cache() -> None:
    """Esquece o que foi memoizado (testes; nenhum caminho de produção precisa)."""
    _MEMO.clear()


# --- leitura ------------------------------------------------------------------


def _clean_provider(raw: Any) -> dict[str, int]:
    """As entradas ``{id: janela}`` sãs de um provider. Silencia o resto.

    ``bool`` é excluído de propósito: é subclasse de ``int`` em Python, e
    ``True`` não é uma janela de contexto.
    """
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(model): value
        for model, value in raw.items()
        if isinstance(model, str)
        and model
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    }


def _clean(raw: Any) -> dict[str, dict[str, int]]:
    """``{provider: {id: janela}}`` validado entrada a entrada. Nunca levanta."""
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, int]] = {}
    for provider, models in raw.items():
        if not isinstance(provider, str) or not provider:
            continue
        cleaned = _clean_provider(models)
        if cleaned:
            out[provider] = cleaned
    return out


def _read_disk(path: Path) -> dict[str, dict[str, int]]:
    """Parse defensivo do arquivo. ``{}`` para ausente/corrompido/gigante."""
    text = safeio.read_text_bounded(path, MAX_FILE_BYTES)
    if not text:
        return {}
    try:
        return _clean(json.loads(text))
    except (ValueError, TypeError):
        # Um json truncado por uma escrita morta na metade não é um incidente:
        # o próximo `lohra models` reescreve por cima.
        return {}


def load_windows(home: Path | str | None = None) -> dict[str, dict[str, int]]:
    """O cache inteiro. **Somente leitura** — o dict é compartilhado pelo memo.

    Consumidores devem preferir ``lookup``, que não expõe a estrutura.
    """
    path = windows_path(home)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        # Ausente/inacessível: nada a memoizar (um stat falho é barato, e assim
        # a PRIMEIRA escrita depois disto é vista na hora).
        return {}
    cached = _MEMO.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]
    data = _read_disk(path)
    _MEMO[path] = (signature, data)
    return data


def lookup(provider: str, model: str, *, home: Path | str | None = None) -> int | None:
    """A janela conhecida de ``model`` neste provider, ou ``None``.

    ``None`` é a resposta honesta para "nunca rodei `lohra models`" e para "esta
    fonte não publica a janela" — quem chama cai no piso do perfil.
    """
    if not provider or not model:
        return None
    return load_windows(home).get(provider, {}).get(model)


# --- escrita ------------------------------------------------------------------


def _merged(
    current: dict[str, dict[str, int]], fresh: Mapping[str, Mapping[str, int]]
) -> dict[str, dict[str, int]]:
    """Merge por provider, leitura nova por cima, imutável (cópias, nunca mutação).

    Um ``lohra models --provider together`` não pode apagar o que já se sabia
    sobre a openrouter; dentro de um provider, o read mais recente ganha.
    """
    out = {name: dict(models) for name, models in current.items()}
    for provider, models in fresh.items():
        cleaned = _clean_provider(models)
        if not cleaned:
            continue
        # Recentes primeiro (e recentes GANHAM): se o teto cortar, quem sobra é
        # o que acabou de ser lido, não o resíduo de meses atrás.
        known = out.get(provider, {})
        combined = {**cleaned, **{k: v for k, v in known.items() if k not in cleaned}}
        if len(combined) > MAX_MODELS_PER_PROVIDER:
            combined = dict(list(combined.items())[:MAX_MODELS_PER_PROVIDER])
        out[provider] = combined
    return out


def remember_windows(
    fresh: Mapping[str, Mapping[str, int]], *, home: Path | str | None = None
) -> bool:
    """Funde ``{provider: {id: janela}}`` no cache. ``True`` se gravou.

    Best-effort de ponta a ponta: qualquer falha vira um log de debug e um
    ``False``. Escrita atômica (tmp no mesmo diretório + ``os.replace``), como
    todo estado durável do repo.
    """
    if not any(_clean_provider(models) for models in fresh.values()):
        return False  # nada a aprender: não toca o disco
    path = windows_path(home)
    try:
        payload = _merged(_read_disk(path), fresh)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".model_windows-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        except BaseException:
            # Um tmp órfão num home do usuário é lixo permanente; a falha em si
            # já é tratada acima.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception:  # noqa: BLE001 — um cache é uma otimização, nunca um bloqueio
        logger.debug("could not persist model context windows", exc_info=True)
        return False
    return True
