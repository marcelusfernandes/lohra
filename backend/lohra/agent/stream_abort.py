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
- **stream em SILÊNCIO**. Este é o residual grande, e é maior do que "ainda não
  começou": o check roda no topo do corpo do ``for``, ou seja, só quando o
  PRÓXIMO evento chega. A latência do abort não é constante — é o intervalo até
  o próximo evento, com teto no read timeout do HTTP
  (``LOHRA_PROVIDER_READ_TIMEOUT``). Um provider que gera continuamente aborta
  em milissegundos; um que fica pensando em silêncio por minutos só é
  interrompido quando voltar a falar (ou quando o read timeout derrubar a
  conexão). Nada aqui olha o relógio entre eventos.

  **Codex/`-sol` — fechado pela issue #59, com um gap nomeado:**
  ``providers/transports/responses.py`` montava ``reasoning`` só com ``effort``.
  A medição ao vivo (2026-09-03) corrigiu a suspeita registrada aqui antes dela:
  o backend NÃO fica mudo durante o raciocínio — ele emite a fronteira
  (``output_item.added/done``) de cada item de reasoning, ~13 eventos em ~40 s.
  O que faltava era resolução, não o primeiro pulso, e é ela que decide a
  latência do abort na fase em que o zumbi do run v4 ficou vivo. Agora o
  transport pede
  ``reasoning: {effort, summary}`` e ``assemble_responses_stream`` consome
  ``response.reasoning_summary_text.delta``: a mesma fase passou a 29–49 eventos
  e a espera esperada por um cancel caiu de ~4,5 s para ~2,3 s (medido). O teto
  continua sendo do modelo, que escolhe o espaçamento das partes do summary —
  nada aqui olha o RELÓGIO entre eventos. **O gap que sobra**: ``summary`` só é pedido
  JUNTO com ``effort`` (um modelo que não raciocina dá 400 no campo), então uma
  leaf autorada SEM ``effort`` continua sem evento durante o raciocínio e mantém
  a latência antiga. O operador pode desligar tudo com
  ``LOHRA_RESPONSES_REASONING_SUMMARY=off`` (volta ao byte anterior);
- **tool em voo**: um ``terminal`` longo (ou um ``write_file``) que já começou
  roda até o fim. O interrupt é lido ANTES do dispatch de um lote, nunca dentro
  de uma tool; ``workflow.quiescence`` é o que torna esse residual visível.

E dois residuais da CONTABILIDADE, onde o contador chega mas a prosa não:

- a cláusula ``stream aborted on cancel; provider usage unknown`` é escrita nos
  dois faults que reportam UM leaf (o timeout escalar do engine e
  ``note_leaf_failure``). O fault agregado da barreira do pipeline
  (``N leaf(s) cancelled``) e os faults administrativos de uma pausa por quota
  não a carregam — aqueles leaves ainda incrementam
  ``usage_uncertain_leaves``, mas o texto não diz por quê;
- **contabilizar um leaf que ainda não assentou perde a incerteza** (pré-existe
  a esta fatia para os TOKENS; agora também vale para o contador).
  ``engine.account_leaf`` deduplica por ``sub_id`` na PRIMEIRA chamada e lê o
  leaf com ``collect(wait=False)``. Se essa primeira leitura pegar o leaf ainda
  ``running``, ela grava 0 tokens e ``usage_uncertain=False`` como FATO, e o
  dedup barra para sempre a leitura correta que viria depois. Reproduzido: com
  o leaf dentro do stream, ``_cancel_inflight()`` seguido de
  ``account_leaf()`` deixa o contador em 0 — meio segundo depois o leaf está
  ``interrupted`` com ``usage_uncertain=True``, e o contador continua 0. Em
  produção o caminho alcançável é ``_timed_out`` quando a espera de quiescência
  EXPIRA (leaf não-abortável: tool em voo ou chamada não-streaming); os demais
  chamadores contabilizam depois de um collect bloqueante, ou só o caso
  ``cancelled == "queued"``, que nunca chegou ao provider. O conserto mexe no
  dedup (só travar em status terminal) e portanto no contrato não-bloqueante
  dos ``on_done`` — outra fatia;
- em fan-out, ``leaves_cost`` soma os leaves de um nó numa ÚNICA linha de
  ``workflow_node_cost``: se um deles foi abortado, a linha guarda um piso sem
  marca por célula (o contador é de RUN, não de célula). Nenhuma estimativa é
  gravada — a restrição dura continua valendo —, mas o "tokens saved" de um
  resume dessa célula subestima.
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


def abort_gate(abort_check: AbortCheck | None) -> AbortCheck:
    """Um portão de abort por CHAMADA de assembler: protegido e com latch.

    Protegido: o predicado vem do loop e lê estado vivo do agente; se ele mesmo
    levantar, a leitura falhou — e "não sei" nunca pode virar "aborte", porque
    abortar descarta uma resposta que o usuário pagou. Fail-open é o lado
    seguro: o turno segue e o interrupt ainda será lido no topo da próxima
    iteração.

    Com LATCH porque o portão é consultado UMA VEZ POR EVENTO: um check
    quebrado num stream de mil eventos despejava mil tracebacks idênticos no
    log — o suficiente para afogar o diagnóstico do turno que realmente
    importa. Depois da primeira falha ele loga uma vez e passa a tratar o check
    como ausente (nem chama de novo): um predicado que levantou não melhora
    sozinho, e re-chamá-lo só paga o custo da exceção.

    O estado vive no closure, ou seja, é por chamada — dois streams
    concorrentes (o pool do core) nunca compartilham o latch, e nenhum estado
    de módulo vaza de um turno para o próximo.
    """
    if abort_check is None:
        return lambda: False
    broken = False

    def gate() -> bool:
        nonlocal broken
        if broken:
            return False
        try:
            return bool(abort_check())
        except Exception:  # noqa: BLE001 — ver docstring: "não sei" ≠ "aborte"
            broken = True
            logger.warning(
                "stream abort: abort_check raised; ignoring it for the rest of "
                "this stream (the turn continues, and the interrupt is still "
                "read between iterations)",
                exc_info=True,
            )
            return False

    return gate
