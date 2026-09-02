"""O interrupt é lido ANTES de despachar tool calls (issues #42-A / #8).

Antes desta fatia o loop checava `agent._interrupt_requested` só no TOPO da
iteração: uma resposta com `tool_calls` que chegasse DEPOIS do cancel era
despachada assim mesmo. Na run real `42abc3eb…` isso virou um zumbi que rodou
`terminal` 156 s depois do nó sucessor já ter começado — o cancel cooperativo
nunca alcançava o único ponto do loop com efeito colateral.

Os testes abaixo são discriminadores: cada um FALHA no código antigo.
Gates/eventos são liberados em `finally` para que uma asserção quebrada nunca
pendure a suíte.
"""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.quiescence import await_quiescence
from tests.test_loop import FakeClient, _text_response, _tool_call_response


def _with_usage(response, *, input_tokens=11, output_tokens=7):
    """Cópia da resposta com usage do provider (nunca muta a original)."""
    return {
        **response,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class _Spy:
    """Dispatch espião: conta e devolve JSON; nunca é chamado num turno morto."""

    def __init__(self):
        self.call_count = 0
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name, args):
        self.call_count += 1
        self.calls.append((name, args))
        return '{"ok": true}'


class _InterruptingClient(FakeClient):
    """Dispara o interrupt DENTRO da chamada ao provider (o caso real).

    O cancel chega enquanto o leaf está no round-trip HTTP: o flag já está de
    pé quando a resposta com `tool_calls` volta.
    """

    def __init__(self, responses, agent_box):
        super().__init__(responses)
        self._agent_box = agent_box

    def create(self, **kwargs):
        self._agent_box[0].request_interrupt()
        return super().create(**kwargs)


def _agent(client, **overrides):
    kwargs = dict(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=client,
    )
    kwargs.update(overrides)
    return Agent(**kwargs)


def _dangling_tool_uses(messages):
    """IDs de tool_call do assistant sem um role:'tool' pareado."""
    results = {
        m.get("tool_call_id") for m in messages if m.get("role") == "tool"
    }
    dangling = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            if call["id"] not in results:
                dangling.append(call["id"])
    return dangling


# --- (i) o dispatch nunca acontece -------------------------------------------


def test_interrupt_during_the_call_stops_the_dispatch():
    box: list[Agent] = []
    spy = _Spy()
    agent = _agent(
        _InterruptingClient(
            [_with_usage(_tool_call_response([("tc_1", "terminal", {"cmd": "rm -rf x"})]))],
            box,
        ),
        tool_dispatch=spy,
        tool_definitions=({"type": "function", "function": {"name": "terminal"}},),
    )
    box.append(agent)

    result = run_conversation(agent, "faça a coisa")

    assert spy.call_count == 0  # falha hoje: a tool roda depois do cancel
    assert result["interrupted"] is True


def test_forced_fallback_response_is_also_gated():
    """Provider que IGNORA o tool_choice forçado cai no caminho de texto — e
    pode devolver tool_calls reais, que chegariam ao mesmo dispatch."""
    box: list[Agent] = []
    spy = _Spy()
    agent = _agent(
        _InterruptingClient(
            [_tool_call_response([("tc_1", "read_file", {"path": "a"})])],
            box,
        ),
        tool_dispatch=spy,
        tool_definitions=({"type": "function", "function": {"name": "read_file"}},),
        forced_tool={
            "type": "function",
            "function": {"name": "final_answer", "parameters": {"type": "object"}},
        },
    )
    box.append(agent)

    result = run_conversation(agent, "responda estruturado")

    assert spy.call_count == 0
    assert result["forced_fallback"] is True
    assert result["interrupted"] is True
    assert _dangling_tool_uses(result["messages"]) == []


# --- (ii) história replay-safe ------------------------------------------------


def test_interrupted_turn_leaves_no_unpaired_tool_use():
    box: list[Agent] = []
    agent = _agent(
        _InterruptingClient(
            [_with_usage(_tool_call_response([("tc_1", "t", {}), ("tc_2", "t", {})], text="vou usar tools"))],
            box,
        ),
        tool_dispatch=_Spy(),
        tool_definitions=({"type": "function", "function": {"name": "t"}},),
    )
    box.append(agent)

    result = run_conversation(agent, "oi")

    assert _dangling_tool_uses(result["messages"]) == []
    # o turno assistant descartado inteiro: a história termina na user message
    assert [m["role"] for m in result["messages"]] == ["user"]
    assert result["messages"][-1]["content"] == "oi"


def test_history_before_the_interrupted_iteration_is_kept_intact():
    """Iteração 1 despacha normalmente; o cancel chega na 2ª chamada."""
    box: list[Agent] = []
    spy = _Spy()
    calls = {"n": 0}

    class _SecondCallInterrupts(FakeClient):
        def create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                box[0].request_interrupt()
            return super().create(**kwargs)

    agent = _agent(
        _SecondCallInterrupts(
            [
                _tool_call_response([("tc_1", "t", {"i": 1})]),
                _tool_call_response([("tc_2", "t", {"i": 2})]),
            ]
        ),
        tool_dispatch=spy,
        tool_definitions=({"type": "function", "function": {"name": "t"}},),
    )
    box.append(agent)

    result = run_conversation(agent, "oi")

    assert spy.call_count == 1  # só a tool da 1ª iteração
    assert [m["role"] for m in result["messages"]] == ["user", "assistant", "tool"]
    assert _dangling_tool_uses(result["messages"]) == []


# --- (iii) status e contabilidade ---------------------------------------------


def test_status_and_usage_match_the_top_of_loop_interrupt():
    box: list[Agent] = []
    agent = _agent(
        _InterruptingClient(
            [_with_usage(_tool_call_response([("tc_1", "t", {})]))],
            box,
        ),
        tool_dispatch=_Spy(),
        tool_definitions=({"type": "function", "function": {"name": "t"}},),
    )
    box.append(agent)

    result = run_conversation(agent, "oi")

    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["partial"] is False
    assert result["final_response"] is None
    assert result["error"] is None
    assert result["error_kind"] is None
    # a chamada ao provider ACONTECEU e custou: a contabilidade fica de pé
    assert result["api_calls"] == 1
    assert result["usage"].input_tokens == 11
    assert result["usage"].output_tokens == 7
    assert result["usage_total"].input_tokens == 11
    # e o flag é consumido pelo turno que ele interrompeu
    assert agent._interrupt_requested is False


# --- (iv) sem interrupt: nada muda --------------------------------------------


def test_without_interrupt_the_turn_is_byte_identical():
    spy = _Spy()
    agent = _agent(
        FakeClient(
            [
                _tool_call_response([("tc_1", "t", {"path": "a.txt"})], text="lendo"),
                _text_response("pronto"),
            ]
        ),
        tool_dispatch=spy,
        tool_definitions=({"type": "function", "function": {"name": "t"}},),
    )

    result = run_conversation(agent, "oi")

    assert spy.call_count == 1
    assert result["messages"] == [
        {"role": "user", "content": "oi"},
        {
            "role": "assistant",
            "content": "lendo",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "tc_1",
                    "type": "function",
                    "function": {"name": "t", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        {
            "role": "tool",
            "name": "t",
            "tool_call_id": "tc_1",
            "content": '{"ok": true}',
        },
        {"role": "assistant", "content": "pronto", "finish_reason": "stop"},
    ]
    assert result["completed"] is True
    assert result["interrupted"] is False


# --- (v) caminho REAL: core.cancel durante a chamada --------------------------


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class _GatedClient(FakeClient):
    """Fica DENTRO da chamada ao provider até o teste liberar o portão."""

    def __init__(self, responses, entered, gate):
        super().__init__(responses)
        self._entered = entered
        self._gate = gate

    def create(self, **kwargs):
        if not self._entered.is_set():
            self._entered.set()
            self._gate.wait(5)
        return super().create(**kwargs)


def test_core_cancel_during_the_call_reaches_quiescence(db):
    entered, gate = threading.Event(), threading.Event()
    spy = _Spy()

    def factory() -> Agent:
        return _agent(
            _GatedClient(
                [_tool_call_response([("tc_1", "terminal", {"cmd": "sleep 900"})])],
                entered,
                gate,
            ),
            tool_dispatch=spy,
            tool_definitions=({"type": "function", "function": {"name": "terminal"}},),
        )

    core = OrchestrationCore(db, factory)
    try:
        sub_id = core.spawn("trabalhe no working_root compartilhado")
        assert entered.wait(5), "o leaf nunca entrou na chamada ao provider"
        assert core.cancel(sub_id) == {"ok": True, "cancelled": "running"}
    finally:
        gate.set()  # solta o provider mesmo se uma asserção quebrar

    try:
        report = await_quiescence(core, [sub_id], timeout_s=5.0)
        assert report.clean is True
        assert report.still_alive == ()
        assert core.collect(sub_id)["status"] == "interrupted"
        assert spy.call_count == 0  # o zumbi não escreveu nada
    finally:
        core.shutdown()
