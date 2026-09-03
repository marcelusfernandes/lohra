"""Abort do consumo do stream no interrupt (issue #42, épico E3).

Até aqui o interrupt era lido em DOIS pontos do turno (topo da iteração e antes
do dispatch de tool calls) — ambos DEPOIS de o round-trip terminar. Um leaf
dentro de uma chamada ao provider só assentava quando o provider terminasse de
gerar: o zumbi da run 42abc3eb ficou 156 s vivo. Estes testes fixam o contrato
novo: entre eventos do stream o consumidor olha o flag, FECHA o stream (para o
servidor parar de gerar) e devolve o sentinela ``AbortedStream``.

A contabilidade é o outro metade do contrato: um stream abortado NÃO reporta
usage 0 como fato — o provider pode ter faturado o que gerou até ali —, então o
turno carrega ``usage_uncertain``.
"""

import logging
import threading
import time
from types import SimpleNamespace

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import (
    ModelClient,
    assemble_anthropic_stream,
    assemble_responses_stream,
    assemble_streamed_response,
)
from lohra.agent.loop import run_conversation
from lohra.agent.stream_abort import AbortedStream, close_stream, is_aborted
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import quiescence
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.quiescence import await_quiescence
from lohra.workflow.rollup import summarize
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_workflow_durable_state import _service
from tests.test_workflow_pipeline import ScriptedClient


class RecordingStream:
    """Um stream de SDK falso: itera o script e REGISTRA o close()."""

    def __init__(self, events):
        self._events = list(events)
        self.closed = 0
        self.consumed = 0

    def __iter__(self):
        for event in self._events:
            self.consumed += 1
            yield event

    def close(self):
        self.closed += 1


class AnthropicStream(RecordingStream):
    def __init__(self, events, final=None):
        super().__init__(events)
        self._final = final if final is not None else {"content": [], "stop_reason": "end_turn"}

    def get_final_message(self):
        return self._final


def _chunk(text=None, *, tool=None, finish=None, usage=None):
    delta = {"content": text}
    if tool is not None:
        delta["tool_calls"] = [tool]
    return {"choices": [{"delta": delta, "finish_reason": finish}], "usage": usage}


def _text_event(delta):
    return SimpleNamespace(
        type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=delta)
    )


def _tool_event(partial):
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


class _AfterN:
    """Um abort_check que vira True depois de N consultas."""

    def __init__(self, n):
        self._n = n
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls > self._n


# --- o sentinela e o close ---------------------------------------------------


def test_the_sentinel_is_recognisable_and_carries_nothing_else():
    aborted = AbortedStream()
    assert aborted.aborted is True
    assert is_aborted(aborted) is True
    assert is_aborted({"choices": []}) is False
    assert is_aborted(None) is False


def test_close_stream_is_best_effort():
    """Um close que explode (ou nem existe) nunca pode matar o unwinding do
    turno: o abort já decidiu que a resposta será descartada."""

    class Exploding:
        def close(self):
            raise RuntimeError("boom")

    close_stream(Exploding())  # não levanta
    close_stream(object())  # sem close(): no-op
    close_stream(None)


def test_a_check_that_raises_never_aborts_the_turn(caplog):
    """Fail-OPEN, e de propósito: abortar descarta uma resposta que o usuário
    pagou. "Não sei" nunca pode virar "aborte" — o interrupt ainda será lido no
    topo da próxima iteração.

    E com LATCH: o portão é consultado uma vez por EVENTO, então um check
    quebrado num stream longo despejava um traceback por evento (medido: 4
    eventos, 4 tracebacks) e afogava o diagnóstico do turno. Loga UMA vez e
    depois trata o check como ausente."""
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("flag ilegível")

    stream = RecordingStream(
        [_chunk("o"), _chunk("i"), _chunk("!"), _chunk(finish="stop")]
    )
    with caplog.at_level(logging.WARNING, logger="lohra.agent.stream_abort"):
        out = assemble_streamed_response(stream, abort_check=boom)

    assert not is_aborted(out)
    assert out["choices"][0]["message"]["content"] == "oi!"
    warnings = [r for r in caplog.records if "abort_check raised" in r.message]
    assert len(warnings) == 1  # uma vez, não uma por evento
    assert len(calls) == 1  # e nem chamado de novo: não melhora sozinho


def test_the_latch_is_per_call_not_per_process():
    """O latch mora no closure do ``abort_gate``, então um stream com o check
    quebrado não pode calar o abort do PRÓXIMO (nem o de um stream concorrente
    no pool do core)."""

    def boom():
        raise RuntimeError("quebrado")

    assemble_streamed_response(RecordingStream([_chunk("a")]), abort_check=boom)
    fresh = RecordingStream([_chunk("a"), _chunk("b"), _chunk(finish="stop")])
    assert is_aborted(assemble_streamed_response(fresh, abort_check=_AfterN(0)))


# --- consumidor 1: chat_completions (chunks) ---------------------------------


def test_chat_completions_stream_closes_and_aborts_between_chunks():
    stream = RecordingStream([_chunk("a"), _chunk("b"), _chunk("c"), _chunk(finish="stop")])
    out = assemble_streamed_response(stream, abort_check=_AfterN(2))
    assert is_aborted(out)
    assert stream.closed == 1
    assert stream.consumed < 4  # parou de ler antes do fim


def test_chat_completions_abort_reaches_a_tool_call_stream():
    """O zumbi da issue era uma resposta de TOOL CALL: nenhum delta de texto.
    O check mora no topo do corpo do for, antes de qualquer filtro por tipo."""
    tool = {"index": 0, "id": "c1", "function": {"name": "terminal", "arguments": "{}"}}
    stream = RecordingStream([_chunk(tool=tool), _chunk(tool=tool), _chunk(finish="tool_calls")])
    out = assemble_streamed_response(stream, abort_check=_AfterN(1))
    assert is_aborted(out)
    assert stream.closed == 1


def test_chat_completions_without_abort_is_byte_identical():
    script = [_chunk("oi"), _chunk(finish="stop", usage={"prompt_tokens": 3})]
    baseline = assemble_streamed_response(RecordingStream(script))
    with_check = assemble_streamed_response(
        RecordingStream(script), abort_check=lambda: False
    )
    assert baseline == with_check
    assert baseline["choices"][0]["message"]["content"] == "oi"
    assert baseline["usage"] == {"prompt_tokens": 3}


def test_the_assembler_closes_whatever_it_consumed():
    """Quem consome o iterador é dono de fechá-lo — em QUALQUER saída. O
    ``close()`` é idempotente e o SDK já o chama ao ler o corpo até o fim; o que
    o ``finally`` compra é a saída por EXCEÇÃO, que antes deixava a conexão
    aberta até o read timeout recolher (BAIXO-4)."""
    stream = RecordingStream([_chunk("oi"), _chunk(finish="stop")])
    out = assemble_streamed_response(stream, abort_check=lambda: False)
    assert not is_aborted(out)
    assert stream.closed == 1


def test_a_protocol_error_closes_the_stream_before_raising():
    """O `raise` de dentro do fold também é uma saída."""
    partial = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": None, "function": {"name": "t", "arguments": "{}"}}
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    stream = RecordingStream([partial, _chunk(finish="tool_calls")])
    with pytest.raises(ValueError, match="incomplete tool-call stream"):
        assemble_streamed_response(stream)
    assert stream.closed == 1


# --- consumidor 2: anthropic (events) ----------------------------------------


def test_anthropic_stream_closes_and_aborts_between_events():
    stream = AnthropicStream([_text_event("a"), _text_event("b"), _text_event("c")])
    out = assemble_anthropic_stream(stream, abort_check=_AfterN(1))
    assert is_aborted(out)
    assert stream.closed == 1


def test_anthropic_abort_reaches_a_tool_call_stream():
    stream = AnthropicStream([_tool_event("{"), _tool_event('"a":1}')])
    out = assemble_anthropic_stream(stream, abort_check=_AfterN(0))
    assert is_aborted(out)
    assert stream.closed == 1
    assert stream.consumed <= 1


def test_anthropic_without_abort_returns_the_final_message():
    seen: list[str] = []
    thoughts: list[str] = []
    thinking = SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="thinking_delta", thinking="hmm"),
    )
    lifecycle = SimpleNamespace(type="content_block_start", delta=None)  # ignorado
    stream = AnthropicStream(
        [lifecycle, _text_event("oi"), thinking], final={"stop_reason": "end_turn"}
    )
    out = assemble_anthropic_stream(
        stream, on_text=seen.append, on_reasoning=thoughts.append, abort_check=lambda: False
    )
    assert out == {"stop_reason": "end_turn"}
    assert seen == ["oi"]
    assert thoughts == ["hmm"]
    assert stream.closed == 0


# --- consumidor 3: responses (events) ----------------------------------------


def _responses_events():
    return [
        SimpleNamespace(type="response.output_text.delta", delta="a"),
        SimpleNamespace(type="response.output_text.delta", delta="b"),
        SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(status="completed", output=[], usage={"total": 1}),
        ),
    ]


def test_responses_stream_closes_and_aborts_between_events():
    stream = RecordingStream(_responses_events())
    out = assemble_responses_stream(stream, abort_check=_AfterN(1))
    assert is_aborted(out)
    assert stream.closed == 1
    assert stream.consumed < 3


def test_responses_without_abort_is_byte_identical():
    baseline = assemble_responses_stream(RecordingStream(_responses_events()))
    with_check = assemble_responses_stream(
        RecordingStream(_responses_events()), abort_check=lambda: False
    )
    assert baseline == with_check
    assert baseline["status"] == "completed"


def test_responses_abort_beats_a_late_failure_event():
    """Abortado é abortado: um response.failed que chegasse DEPOIS do abort não
    vira erro do turno — ninguém leu o stream até lá."""
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="a"),
        SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(error=SimpleNamespace(code="x", message="y")),
        ),
    ]
    stream = RecordingStream(events)
    assert is_aborted(assemble_responses_stream(stream, abort_check=_AfterN(1)))
    assert stream.closed == 1


def test_a_failure_event_still_raises_when_nothing_aborted():
    from lohra.providers.errors import ProviderCallFailed

    events = [
        SimpleNamespace(
            type="response.failed",
            response=SimpleNamespace(error=SimpleNamespace(code="rate", message="slow down")),
        )
    ]
    stream = RecordingStream(events)
    with pytest.raises(ProviderCallFailed):
        assemble_responses_stream(stream, abort_check=lambda: False)
    # BAIXO-4: o raise vinha de DENTRO do for e saía sem fechar.
    assert stream.closed == 1


# --- o turno: o loop termina interrompido, sem mensagem assistant ------------


class EndlessStream:
    """Um stream que nunca acaba — só o abort tira o turno de dentro dele.

    ``on_chunk`` é o cancel chegando de fora no meio da geração (aqui
    determinístico, sem thread). O teto existe para que uma regressão no abort
    falhe o teste em vez de pendurar a suíte."""

    LIMIT = 500

    def __init__(self, on_chunk=None):
        self.closed = 0
        self.emitted = 0
        self._on_chunk = on_chunk

    def __iter__(self):
        while True:
            self.emitted += 1
            if self.emitted > self.LIMIT:
                raise AssertionError("the stream was never aborted")
            if self._on_chunk is not None:
                self._on_chunk(self.emitted)
            yield _chunk("mais texto")

    def close(self):
        self.closed += 1


class StreamingClient(ModelClient):
    """Respostas roteirizadas; um ``EndlessStream`` no lugar de uma delas."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []
        self.streams: list[EndlessStream] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, EndlessStream):  # pragma: no cover - guarda de uso
            raise AssertionError("um EndlessStream só é servido pelo caminho stream()")
        return item

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        if self._script and isinstance(self._script[0], EndlessStream):
            self.calls.append(kwargs)
            stream = self._script.pop(0)
            self.streams.append(stream)
            return assemble_streamed_response(
                stream, on_text=on_text, on_reasoning=on_reasoning, abort_check=abort_check
            )
        return self.create(**kwargs)


def _openai_text(text, *, usage=None):
    """Uma resposta chat_completions não-streaming (o transport do openrouter)."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
    }


def _agent(client):
    return Agent(
        model="test-model", provider=get_provider_profile("openrouter"), client=client
    )


def test_an_interrupt_mid_stream_ends_the_turn_with_no_assistant_message():
    """O contrato do épico E3, no caminho do loop: o cancel chega DENTRO do
    round-trip, o stream é fechado, e o turno tem o mesmo shape do interrupt
    lido antes do dispatch — sem turno assistant pendurado."""
    box: dict = {}
    stream = EndlessStream(
        on_chunk=lambda n: box["agent"].request_interrupt() if n == 2 else None
    )
    client = StreamingClient([stream])
    agent = _agent(client)
    box["agent"] = agent

    deltas: list[str] = []
    result = run_conversation(agent, "vai", stream_delta_callback=deltas.append)

    assert result["interrupted"] is True
    assert result["usage_uncertain"] is True
    assert result["completed"] is False
    assert result["partial"] is False
    assert result["final_response"] is None
    assert result["error"] is None and result["error_kind"] is None
    assert result["api_calls"] == 1  # a chamada ACONTECEU e custou
    assert [m["role"] for m in result["messages"]] == ["user"]
    assert stream.closed == 1
    assert stream.emitted < EndlessStream.LIMIT
    assert deltas  # o que já tinha chegado foi entregue ao vivo, e só


def test_an_aborted_stream_invents_no_usage():
    """RESTRIÇÃO DURA: sem ``usage`` (ele só chega no fim do stream), o turno
    não fabrica números — nem 0 como fato, nem estimativa."""
    box: dict = {}
    stream = EndlessStream(on_chunk=lambda n: box["agent"].request_interrupt())
    agent = _agent(StreamingClient([stream]))
    box["agent"] = agent

    result = run_conversation(agent, "vai", stream_delta_callback=lambda _t: None)

    assert result["usage"] is None
    assert result["usage_total"] is None
    assert result["usage_uncertain"] is True


def test_the_usage_of_earlier_round_trips_survives_as_a_floor():
    """Um turno de várias chamadas abortado na última mantém o que as
    anteriores REPORTARAM — é um piso honesto, marcado como incompleto."""
    box: dict = {}
    stream = EndlessStream(on_chunk=lambda n: box["agent"].request_interrupt())
    first = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
    }
    client = StreamingClient([first, stream])
    agent = Agent(
        model="test-model",
        provider=get_provider_profile("openrouter"),
        client=client,
        tool_dispatch=lambda name, args: "{}",
        tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
    )
    box["agent"] = agent

    result = run_conversation(agent, "vai", stream_delta_callback=lambda _t: None)

    assert result["interrupted"] is True
    assert result["usage_uncertain"] is True
    assert result["api_calls"] == 2
    assert result["usage_total"].input_tokens == 11  # exatamente o reportado
    assert result["usage_total"].output_tokens == 5
    # a 1ª rodada e seus tool_results ficam; a 2ª (abortada) não anexa nada
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "tool"]


def test_without_an_interrupt_a_streamed_turn_is_unchanged():
    """(iii) Byte-identidade: mesmas mensagens e mesmo usage que o caminho
    não-streaming; ``usage_uncertain`` False."""
    payload = _openai_text("pronto", usage={"prompt_tokens": 7, "completion_tokens": 2})
    streamed = run_conversation(
        _agent(StreamingClient([payload])), "vai", stream_delta_callback=lambda _t: None
    )
    plain = run_conversation(_agent(StreamingClient([payload])), "vai")

    assert streamed["messages"] == plain["messages"]
    assert streamed["usage"] == plain["usage"]
    assert streamed["usage_total"] == plain["usage_total"]
    assert streamed["final_response"] == plain["final_response"] == "pronto"
    assert streamed["usage_uncertain"] is False
    assert plain["usage_uncertain"] is False


# --- o caminho REAL: core.spawn + cancel durante o stream --------------------


class SlowEndlessStream:
    """Como o ``EndlessStream``, mas com o ritmo de um provider gerando: dá
    tempo de uma OUTRA thread cancelar no meio (é o cenário da issue)."""

    LIMIT = 2000

    def __init__(self, streaming: threading.Event):
        self.closed = 0
        self.emitted = 0
        self._streaming = streaming

    def __iter__(self):
        while True:
            self.emitted += 1
            if self.emitted > self.LIMIT:  # pragma: no cover - guarda anti-hang
                raise AssertionError("the stream was never aborted")
            self._streaming.set()
            time.sleep(0.005)
            yield _chunk("tok ")

    def close(self):
        self.closed += 1


class SlowStreamClient(ModelClient):
    """Um provider que só streama, e sem fim."""

    def __init__(self, streaming: threading.Event):
        self._streaming = streaming
        self.streams: list[SlowEndlessStream] = []

    def create(self, **kwargs):  # pragma: no cover - o caminho aqui é o stream
        raise AssertionError("this client only streams")

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        stream = SlowEndlessStream(self._streaming)
        self.streams.append(stream)
        return assemble_streamed_response(
            stream, on_text=on_text, on_reasoning=on_reasoning, abort_check=abort_check
        )


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder):
    """Um core de controle: leaves que respondem na hora, sem abort nenhum."""

    def factory():
        return Agent(
            model="test-model",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=2)


def _streaming_core(db, streaming):
    def factory():
        return Agent(
            model="test-model",
            provider=get_provider_profile("openrouter"),
            client=SlowStreamClient(streaming),
        )

    return OrchestrationCore(db, factory, max_concurrent=2)


def test_a_cancel_during_the_stream_reaches_quiescence(db):
    """O discriminador do épico E3, no caminho real. Antes desta fatia o leaf
    ficava dentro do round-trip até o provider terminar de gerar — aqui, nunca —
    e ``await_quiescence`` reportava ``still_alive``.

    Discriminação verificada por sabotagem: trocando o ``abort_check`` deste
    client por None, o teste falha com
    ``still_alive=(<sub_id>,), elapsed=5.003`` — os 5,0 s inteiros do cap. Daí o
    teto de 1,0 s abaixo em vez do cap: ele pega também o caso "aborta, mas
    devagar", não só o "não aborta"."""
    streaming = threading.Event()
    core = _streaming_core(db, streaming)
    try:
        sub_id = core.spawn("gera sem parar")
        assert streaming.wait(5), "o leaf nunca entrou no stream"

        started = time.monotonic()
        core.cancel(sub_id)
        report = await_quiescence(core, [sub_id])
        elapsed = time.monotonic() - started

        assert report.clean is True  # antes: still_alive
        assert report.still_alive == ()
        assert elapsed < 1.0 < quiescence.CANCEL_QUIESCENCE_TIMEOUT
        out = core.collect(sub_id)
        assert out["status"] == "interrupted"
        assert out["usage_uncertain"] is True  # o stream morreu antes do usage
        assert out["tokens_in"] == 0 and out["tokens_out"] == 0  # piso, não fato
    finally:
        core.shutdown(wait=False)


def test_a_leaf_that_never_streamed_reports_no_uncertainty(db):
    """Controle: sem abort, ``usage_uncertain`` é False — a conta é exata."""
    core = _core(db, lambda prompt: "pronto")
    try:
        sub_id = core.spawn("trabalha")
        out = core.collect(sub_id, wait=True, timeout=5)
        assert out["status"] == "complete"
        assert out["usage_uncertain"] is False
    finally:
        core.shutdown(wait=False)


# --- o rollup e o fault contam a incerteza à parte ---------------------------


def test_the_rollup_counts_the_uncertain_leaf_and_the_fault_says_why(db):
    """(iv) Um leaf cortado no meio do stream não vira "custou 0": o rollup
    conta ``usage_uncertain_leaves`` e o fault nomeia a incerteza de cobrança."""
    streaming = threading.Event()
    core = _streaming_core(db, streaming)
    spec = validate_spec(
        {
            "meta": {"name": "abort"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "gera", "timeout": 0.3}],
        }
    )
    try:
        engine = WorkflowEngine(core, budget=Budget())
        result = engine.run(spec, {})
        assert result.outputs["a"] is None
        assert result.usage_uncertain_leaves == 1
        faults = "\n".join(result.faults)
        assert "provider usage unknown" in faults
        assert "STILL RUNNING" not in faults  # o abort alcançou a quiescência

        rollup = summarize("run-1", result.status, result)
        assert rollup["usage_uncertain_leaves"] == 1
        assert rollup["tokens_in"] == 0  # o piso, ao lado da ressalva
    finally:
        core.shutdown(wait=False)


def test_a_clean_run_reports_zero_uncertain_leaves(db):
    """0 é uma afirmação positiva ("toda a conta é exata"), não um silêncio."""
    core = _core(db, lambda prompt: "pronto")
    spec = validate_spec(
        {"meta": {"name": "ok"}, "nodes": [{"id": "a", "type": "agent", "prompt": "vai"}]}
    )
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.usage_uncertain_leaves == 0
        assert summarize("run-2", result.status, result)["usage_uncertain_leaves"] == 0
    finally:
        core.shutdown(wait=False)


# --- a incerteza atravessa os estirões (cross-process) ------------------------


_ABORT_THEN_GATE = {
    "meta": {"name": "aborted", "version": 1},
    "nodes": [
        {"id": "gera", "type": "agent", "prompt": "gera sem parar", "timeout": 0.3},
        {"id": "ask", "type": "checkpoint", "prompt": "Segue?"},
    ],
}


def _streaming_service(db, home, streaming):
    def factory():
        return Agent(
            model="test-model",
            provider=get_provider_profile("openrouter"),
            client=SlowStreamClient(streaming),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def test_the_uncertainty_survives_a_pause_and_a_fresh_process(tmp_path):
    """A metade cross-estirão da contabilidade.

    O produtor nº 1 de stream abortado é justamente a pausa (quota cancela os
    leaves em voo) — e o rollup de um resume é calculado sobre um ``RunResult``
    NOVO, que não viu o estirão anterior. Sem carregar o contador, o resume
    emitia ``usage_uncertain_leaves: 0`` — a afirmação positiva "toda a conta é
    exata" — ao lado de um ``tokens_spent_total`` cumulativo que JÁ inclui o
    piso daquele leaf. Duas linhas vizinhas, uma delas mentindo."""
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        streaming = threading.Event()
        svc = _streaming_service(db, tmp_path, streaming)
        try:
            run_id = svc.start(_ABORT_THEN_GATE, {})["run_id"]
            paused = svc.status(run_id, wait=True, timeout=20)
            assert paused["status"] == "paused"  # parou no gate humano
            # O leaf morreu com o stream cortado, e isso é dito AQUI.
            assert paused["usage_uncertain_leaves"] == 1
            assert "provider usage unknown" in "\n".join(paused["faults"])
        finally:
            svc.shutdown()  # o "kill": o registro em memória morre, o SQLite não

        # O que o processo morto deixou na linha é o que o próximo lê.
        line = svc._store.load(run_id)
        assert line.prior_uncertain == 1

        # O estirão 2 roda com um provider SÃO (responde na hora): o nó morto
        # re-spawna e completa, então o contador DESTE segmento é 0. É o
        # discriminador — sem o carry, o rollup final diria 0.
        svc2 = _service(db, tmp_path, lambda prompt: "pronto")
        try:
            durable = svc2.status(run_id)
            assert durable["status"] == "paused"
            assert durable["usage_uncertain_leaves"] == 1

            # E agora o que só o CARRY prova: o estirão 2 responde o gate com um
            # ``RunResult`` novo, cujo contador de segmento é 0. O rollup final
            # tem de dizer 1 — o do RUN — ao lado do total cumulativo de tokens.
            out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "sim"})
            assert "error" not in out, out
            final = svc2.status(run_id, wait=True, timeout=20)
            assert final["status"] in ("complete", "degraded")
            # 1 = 0 (este estirão) + 1 (o anterior). Sem o carry seria 0, ao
            # lado de um tokens_spent_total que já carrega o piso do leaf morto.
            assert final["usage_uncertain_leaves"] == 1
            assert svc2._store.load(run_id).prior_uncertain == 1
        finally:
            svc2.shutdown()
    finally:
        db.close()


def test_a_run_with_no_aborted_leaf_still_reports_zero_across_stretches(tmp_path):
    """Controle: o carry não pode inventar incerteza onde não houve nenhuma."""
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        svc = _service(db, tmp_path, lambda prompt: "pronto")
        try:
            run_id = svc.start(
                {
                    "meta": {"name": "limpo", "version": 1},
                    "nodes": [
                        {"id": "a", "type": "agent", "prompt": "vai"},
                        {"id": "ask", "type": "checkpoint", "prompt": "Segue?"},
                    ],
                },
                {},
            )["run_id"]
            paused = svc.status(run_id, wait=True, timeout=20)
            assert paused["status"] == "paused"
            assert paused["usage_uncertain_leaves"] == 0
        finally:
            svc.shutdown()

        assert svc._store.load(run_id).prior_uncertain == 0
        svc2 = _service(db, tmp_path, lambda prompt: "pronto")
        try:
            assert svc2.status(run_id)["usage_uncertain_leaves"] == 0
        finally:
            svc2.shutdown()
    finally:
        db.close()
