"""Abortar o consumo de um stream quando o interrupt chega (issue #42, épico E3).

O interrupt do agente sempre foi cooperativo e lido em pontos que só existem
ENTRE round-trips: o topo da iteração do loop e — desde a 0.0.20 — a linha antes
do dispatch de tool calls. Nenhum dos dois cobre a janela em que o turno
realmente fica: dentro da chamada ao provider. Um leaf cancelado só assentava
quando o provider terminasse de gerar; na run investigada (42abc3eb) isso foram
156 s, durante os quais o nó sucessor já tinha começado.

Um turno que STREAMA (todo leaf de workflow streama) tem um ponto de leitura a
mais: cada evento entregue. Este módulo dá aos três consumidores de stream de
``client.py`` a mesma primitiva:

- ``AbortCheck`` — a função sem argumentos que o loop passa (lê o flag do agente);
- ``close_stream`` — FECHA o stream de verdade (o ``close()`` do SDK/httpx, que
  derruba a conexão HTTP e faz o servidor parar de gerar). Sair do ``for`` com um
  ``break`` deixaria a resposta escoando no socket até o fim, que é exatamente o
  custo que este épico existe para cortar;
- ``AbortedStream`` — o sentinela que o consumidor devolve no lugar da resposta
  bruta. O loop o reconhece ANTES de normalizar: não há resposta para normalizar,
  nenhuma mensagem assistant é anexada, e o turno termina ``interrupted``, o
  mesmo shape do interrupt lido antes do dispatch.

**Contabilidade (restrição dura).** Usage só chega no FIM de um stream (o chunk
final de ``usage``/o evento terminal). Num stream abortado o uso real é
DESCONHECIDO — o provider pode ter faturado tudo que gerou até o corte —, e
registrar 0 seria afirmar um fato falso. Quem carrega o sentinela carrega também
``usage_uncertain``: o número que sobra no turno é um PISO, e o contador
``usage_uncertain_leaves`` do rollup diz quantas células têm essa ressalva.
Nenhuma estimativa é inventada nem cobrada no ledger exato.

**O que este módulo NÃO aborta** (nomeado de propósito, é o residual desta fatia):

- **chamada não-streaming** (``ModelClient.create``): não há evento intermediário
  onde olhar o flag. Inclui o ``lohra chat --json`` (streaming desligado por
  contrato do envelope), a compactação do ``agent/aux.py`` e o
  ``ResponsesClient.create`` — que streama por dentro (o backend do Codex exige
  ``stream=true``) mas não recebe ``abort_check``;
- **stream que ainda não produziu evento nenhum**: o provider "pensando" em
  silêncio não entrega nada onde o check possa rodar. Quem cobre esse caso é o
  read timeout do HTTP (``LOHRA_PROVIDER_READ_TIMEOUT``), não o interrupt;
- **tool em voo**: um ``terminal`` longo (ou um ``write_file``) que já começou
  roda até o fim. O interrupt é lido ANTES do dispatch de um lote, nunca dentro
  de uma tool; ``workflow.quiescence`` é o que torna esse residual visível.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Lida entre eventos do stream; True = o turno foi interrompido, pare de ler.
AbortCheck = Callable[[], bool]


@dataclass(frozen=True)
class AbortedStream:
    """Resposta normalizada de um stream fechado por interrupt.

    Deliberadamente VAZIA: sem ``final_response``, sem tool_calls, sem usage. O
    que o provider tinha entregue até o corte é parcial por construção — anexá-lo
    como turno assistant deixaria um ``tool_use`` sem par (o mesmo hazard de
    replay que o interrupt pré-dispatch evita descartando o turno).

    Imutável (``frozen``) porque é lida por várias camadas — loop, core, engine —
    e nenhuma delas tem o direito de reescrever o veredito da outra.
    """

    aborted: bool = True


def is_aborted(raw: Any) -> bool:
    """A resposta bruta é o sentinela de abort? (nunca um ``isinstance`` solto
    espalhado pelas camadas — o teste do tipo mora aqui, com o tipo)."""
    return isinstance(raw, AbortedStream)


def close_stream(stream: Any) -> None:
    """Fecha o stream do SDK, best-effort.

    ``close()`` existe nos três (``openai.Stream``, o ``MessageStream`` do
    anthropic, e ambos por baixo fecham a resposta httpx) — mas um objeto sem
    ``close`` (um iterador de teste, um SDK futuro) não pode derrubar o
    unwinding do turno, e um ``close`` que explode ainda menos: a resposta já
    foi descartada, e o pior caso de falhar aqui é a conexão morrer sozinha no
    read timeout. Registrado no log, nunca propagado.
    """
    close = getattr(stream, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:  # noqa: BLE001 — um close quebrado não é um turno quebrado
        logger.warning("stream abort: close() failed; the socket will time out",
                       exc_info=True)


def should_abort(abort_check: AbortCheck | None) -> bool:
    """``abort_check()`` protegido: sem check, nunca aborta.

    O predicado vem do loop e lê estado vivo do agente; se ele mesmo levantar,
    a leitura falhou — e "não sei" nunca pode virar "aborte", porque abortar
    descarta uma resposta que o usuário pagou. Fail-open aqui é o lado seguro:
    o turno segue e o interrupt ainda será lido no topo da próxima iteração.
    """
    if abort_check is None:
        return False
    try:
        return bool(abort_check())
    except Exception:  # noqa: BLE001 — ver docstring: "não sei" ≠ "aborte"
        logger.warning("stream abort: abort_check raised; continuing", exc_info=True)
        return False
