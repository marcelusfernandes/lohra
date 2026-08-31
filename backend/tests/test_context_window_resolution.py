"""De onde o preflight de compactação tira a janela de contexto (issue #38).

O turno 9 do épico Wave 6 morreu com ``stop_reason: length`` vindo direto do
provider, sem uma linha sequer sobre compactação: o loop assumia 200k para TODO
modelo de TODO provider, e o modelo em uso naquela rota OpenRouter tinha bem
menos. O mecanismo de defesa existia e nunca foi consultado.

A ordem que estes testes fixam — override explícito > cache do catálogo > piso do
perfil > 200k — é a única em que "não sei" degrada para conservador em vez de
otimista.
"""

from __future__ import annotations

from lohra.agent.agent import DEFAULT_CONTEXT_WINDOW, Agent
from lohra.agent.aux import AuxClient
from lohra.agent.client import ModelClient
from lohra.agent.context import ContextCompressor
from lohra.agent.loop import run_conversation
from lohra.catalog import windows as win
from lohra.memory.paths import lohra_home
from lohra.providers import get_provider_profile
from lohra.providers.base import ProviderProfile
from lohra.providers.transports import get_transport


def _text_response(text: str) -> dict:
    """Resposta no formato chat_completions (o transport da openrouter)."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }


def _anthropic_response(text: str) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


class _Canned(ModelClient):
    """Devolve respostas programadas e registra o que foi enviado."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("chamado mais vezes do que o programado")
        return self._responses.pop(0)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs):
        return self.create(**kwargs)

    def close(self):
        pass


class _AlwaysSummary(ModelClient):
    """O aux fala anthropic_messages (é como o AuxClient é montado abaixo)."""

    def create(self, **kwargs):
        return _anthropic_response("COMPACTED SUMMARY")


def _agent(responses=(), *, provider="openrouter", model="deepseek/deepseek-v4-pro", **overrides):
    profile = provider if isinstance(provider, ProviderProfile) else get_provider_profile(provider)
    return Agent(model=model, provider=profile, client=_Canned(responses), **overrides)


def _compacting(_shape=_text_response, **overrides) -> Agent:
    """Um agente com o preflight ARMADO (engine + aux), como o `lohra chat`."""
    return _agent(
        [_shape("done")],
        context_engine=ContextCompressor(protect_first_n=2, protect_last_n=2),
        aux_client=AuxClient(
            client=_AlwaysSummary(),
            transport=get_transport("anthropic_messages"),
            model="claude-haiku-4-5",
        ),
        **overrides,
    )


def _long_history(turns: int = 20) -> list[dict]:
    """Histórico grande o bastante para estourar meia janela pequena."""
    out = []
    for i in range(turns):
        if i % 2 == 0:
            out.append({"role": "user", "content": "x" * 400})
        else:
            out.append({"role": "assistant", "content": "y" * 400, "finish_reason": "stop"})
    return out


# --- precedência --------------------------------------------------------------


def test_an_explicit_window_wins_over_everything(tmp_path):
    win.clear_cache()
    win.remember_windows({"openrouter": {"deepseek/deepseek-v4-pro": 64_000}}, home=lohra_home())
    assert _agent(context_window=4_242).resolve_context_window() == 4_242


def test_an_explicit_window_set_after_construction_is_honoured_too():
    # test_loop_inbox.py atribui o campo depois de construir; continua valendo.
    agent = _agent()
    agent.context_window = 777
    assert agent.resolve_context_window() == 777


def test_the_catalog_cache_wins_over_the_profile_floor():
    win.clear_cache()
    floor = get_provider_profile("openrouter").default_context_window
    win.remember_windows({"openrouter": {"deepseek/deepseek-v4-pro": 163_840}}, home=lohra_home())
    agent = _agent()
    assert agent.resolve_context_window() == 163_840 != floor


def test_the_cache_is_read_per_provider_and_per_model():
    win.clear_cache()
    win.remember_windows({"together": {"deepseek/deepseek-v4-pro": 999}}, home=lohra_home())
    # mesmo id, OUTRO provider: não é a mesma janela
    assert _agent().resolve_context_window() == 32_000
    # mesmo provider, outro id
    assert _agent(model="outro/modelo").resolve_context_window() == 32_000


def test_without_a_cache_the_profile_floor_answers():
    win.clear_cache()
    assert _agent().resolve_context_window() == 32_000  # piso da openrouter
    # Modelo NÃO listado no model_windows da anthropic: cai no default_context_window
    # (o piso do perfil), que é o caminho que este teste exercita.
    assert _agent(provider="anthropic", model="claude-legacy-unknown").resolve_context_window() == 200_000
    assert _agent(provider="openai", model="gpt-4o").resolve_context_window() == 128_000


def test_anthropic_resolves_per_model_windows():
    # A anthropic serve 200k E 1M sob o mesmo perfil. O longest-prefix distingue.
    win.clear_cache()

    def w(model):
        return _agent(provider="anthropic", model=model).resolve_context_window()

    assert w("claude-haiku-4-5") == 200_000
    assert w("claude-sonnet-4-5") == 200_000
    assert w("claude-opus-4-5") == 200_000
    assert w("claude-opus-4-5-20251101") == 200_000  # sufixo de data absorvido
    assert w("claude-sonnet-5") == 1_000_000
    assert w("claude-opus-4-6") == 1_000_000  # o prefixo -4-5 NÃO casa -4-6
    assert w("claude-opus-4-8") == 1_000_000


def test_openai_key_based_resolves_per_model_windows():
    # Rota da API-key (developers.openai.com 2026-08-31), DISTINTA do Codex: aqui
    # gpt-5.5 = 1.05M. gpt-5.5 vs gpt-5 é um caso de longest-prefix (gpt-5.5
    # startswith gpt-5) — a prova de que o matcher funciona num 2º provider.
    win.clear_cache()

    def w(model):
        return _agent(provider="openai", model=model).resolve_context_window()

    assert w("gpt-5.5") == 1_050_000  # NÃO 400_000 do prefixo gpt-5
    assert w("gpt-5") == 400_000
    assert w("gpt-4.1") == 1_047_576
    assert w("gpt-4o") == 128_000
    assert w("gpt-4o-mini") == 128_000
    assert w("gpt-desconhecido") == 128_000  # cai no default do perfil


def test_codex_subscription_resolves_the_capped_400k_window():
    # gpt-5.5 tem 1M na API pura, mas o backend Codex/subscription CAPA em 400k
    # total (272k in + 128k out). Antes disso o perfil não declarava janela e
    # caía no fallback de 200k — o assert abaixo falharia antes do fix, mata o
    # turno por `length` se fosse 1M (openai/codex, 2026-08-31).
    from lohra.subscription.provider import CODEX_PROVIDER

    win.clear_cache()
    agent = _agent(provider=CODEX_PROVIDER, model="gpt-5.5")
    assert agent.resolve_context_window() == 400_000


def test_the_catalog_cache_still_beats_the_static_model_windows():
    # Precedência intacta: um lookup no windows.json ganha da tabela estática do
    # perfil, exatamente como ganha do piso único. Sem mudança de código — o
    # cache é consultado ANTES do hook do provider — mas pina a ordem.
    win.clear_cache()
    win.remember_windows({"anthropic": {"claude-opus-4-8": 123_456}}, home=lohra_home())
    agent = _agent(provider="anthropic", model="claude-opus-4-8")
    assert agent.resolve_context_window() == 123_456 != 1_000_000


def test_a_profile_with_no_claim_falls_back_to_the_old_default():
    # ollama é local: nem perfil nem catálogo sabem a janela. O fallback final é
    # exatamente o comportamento antigo — nada regride para quem estava certo.
    win.clear_cache()
    agent = _agent(provider="ollama", model="qwen3:8b")
    assert agent.resolve_context_window() == DEFAULT_CONTEXT_WINDOW == 200_000


def test_a_corrupt_cache_never_breaks_the_resolution():
    win.clear_cache()
    (lohra_home()).mkdir(parents=True, exist_ok=True)
    win.windows_path(lohra_home()).write_text("{lixo")
    assert _agent().resolve_context_window() == 32_000


def test_the_resolution_is_not_frozen_at_construction():
    # O hook `configure` do core troca agent.model por sub-sessão; a janela tem
    # de reacompanhar, e é por isso que o loop resolve A CADA decisão.
    win.clear_cache()
    win.remember_windows(
        {"openrouter": {"pequeno/modelo": 8_000, "grande/modelo": 400_000}}, home=lohra_home()
    )
    agent = _agent(model="pequeno/modelo")
    assert agent.resolve_context_window() == 8_000
    agent.model = "grande/modelo"
    assert agent.resolve_context_window() == 400_000


# --- o cenário do turno 9 -----------------------------------------------------


def test_a_small_openrouter_window_compacts_where_200k_would_have_died():
    """O caso real: janela pequena + histórico grande.

    Com o antigo hardcode de 200k o preflight nunca dispararia (o histórico está
    muito abaixo de 100k tokens) e o turno chegaria ao provider para morrer por
    `length`. Com a janela real, compacta.
    """
    win.clear_cache()
    win.remember_windows({"openrouter": {"deepseek/deepseek-v4-pro": 4_000}}, home=lohra_home())
    history = _long_history()

    agent = _compacting()
    result = run_conversation(agent, "e agora?", conversation_history=history)

    assert result["compacted"] is True
    assert result["final_response"] == "done"
    assert any("COMPACTED SUMMARY" in (m.get("content") or "") for m in result["messages"])
    assert len(agent.client.calls[0]["messages"]) < len(history)


def test_the_same_history_under_the_old_assumption_never_compacts():
    """O discriminador: sem a janela real, o mesmo turno passa reto.

    Se este teste um dia compactar, o de cima deixou de provar o que diz.
    """
    win.clear_cache()
    result = run_conversation(
        _compacting(context_window=200_000), "e agora?", conversation_history=_long_history()
    )
    assert result["compacted"] is False


def test_the_profile_floor_alone_already_defends_a_never_listed_model():
    # Nunca rodou `lohra models`: o piso de 32k da openrouter ainda dispara onde
    # 200k não dispararia — "não sei" degrada para conservador.
    win.clear_cache()
    # 300 turnos de 400 chars ≈ 30k tokens estimados: acima da metade de 32k,
    # ainda MUITO abaixo da metade dos 200k antigos.
    history = _long_history(turns=300)
    assert run_conversation(_compacting(), "e agora?", conversation_history=history)["compacted"]
    # Discriminador: o MESMO histórico sob a suposição antiga passa reto.
    old_assumption = run_conversation(
        _compacting(context_window=200_000), "e agora?", conversation_history=history
    )
    assert old_assumption["compacted"] is False


# --- regressões ---------------------------------------------------------------


def test_no_engine_means_the_window_is_never_even_resolved(monkeypatch):
    # `lohra serve` constrói Agents sem engine/aux; o caminho sem compactação não
    # pode ganhar I/O de arquivo por iteração.
    def refuse(*_args, **_kwargs):
        raise AssertionError("o caminho sem compactação não pode ler o cache")

    monkeypatch.setattr(win, "lookup", refuse)
    result = run_conversation(_agent([_text_response("oi")]), "olá")
    assert result["compacted"] is False
    assert result["final_response"] == "oi"


def test_anthropic_keeps_the_window_it_always_had():
    win.clear_cache()
    # claude-opus-4-5 continua 200k no model_windows — a regressão que este teste
    # guarda (janela grande da anthropic não compacta um histórico pequeno) vale
    # igual; claude-opus-4-8 agora seria 1M, ainda menos motivo pra compactar.
    agent = _compacting(provider="anthropic", model="claude-opus-4-5", _shape=_anthropic_response)
    assert agent.resolve_context_window() == 200_000
    result = run_conversation(agent, "oi", conversation_history=_long_history())
    assert result["compacted"] is False  # 200k de janela: nada a comprimir ainda
