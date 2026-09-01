"""The conversation loop — Phase 1: user -> API -> response, no tools.

Faithful to spec §1 but trimmed to the chat slice: prologue (sanitize, restore
the frozen system prompt, append the user turn), a bounded main loop (check
interrupt, build kwargs, call, normalize, append the assistant turn, branch on
finish_reason), and the result-dict epilogue. Tool dispatch, persistence, and
context compression land in later phases.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from lohra.agent.agent import Agent, ToolDispatch
from lohra.agent.client import TextCallback
from lohra.agent.types import NormalizedResponse, ToolCall, Usage, combine_usage
from lohra.providers.errors import classify_provider_error, retry_after_seconds
from lohra.providers.transports.base import parse_tool_arguments

logger = logging.getLogger(__name__)

_LONE_SURROGATES = re.compile(r"[\ud800-\udfff]")
_MAX_TOOL_WORKERS = 8


def _forced_call_arguments(tool_calls: list[ToolCall] | None, name: str) -> str | None:
    """The raw JSON arguments of the forced tool call (the answer), or None if the
    provider didn't call it — signalling a fall back to the text path (§5.3)."""
    for call in tool_calls or ():
        if call.name == name:
            return call.arguments
    return None

# A steer inbox: called between iterations; returns texts to inject (and clears
# them). See docs/specs/06-orchestration.md §6.
Inbox = Callable[[], list[str]]


_OVERLAY_MARK = "\u279c\u2009contexto operacional do turno\u2009(n\u00e3o \u00e9 fala do usu\u00e1rio):"
_OVERLAY_END = "\u279c\u2009fim do contexto operacional\u2009"


def _apply_request_overlay(messages: list[dict], overlay: str) -> list[dict]:
    """Cópia provider-facing com o overlay embutido na ÚLTIMA user message.

    Operando sobre CÓPIA (e reaplicada do zero a cada API call): a história
    canônica — e o result dict — nunca contém o overlay, e cada chamada do
    turno o vê exatamente uma vez, independente de compactação/steering
    terem substituído a lista entre iterações. Nenhuma user message extra é
    criada (user/user rejeitado por providers) e o system prompt é intocado.
    """
    sent = list(messages)
    for index in range(len(sent) - 1, -1, -1):
        message = sent[index]
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        sent[index] = {
            **message,
            "content": _sanitize_text(
                message["content"] + _OVERLAY_MARK + overlay + _OVERLAY_END
            ),
        }
        return sent
    return sent  # sem user message (não deveria ocorrer): envia como está


def _steer_message(texts: list[str]) -> dict:
    """Merge drained steer texts into ONE user message wrapped as a reminder.

    Merging avoids back-to-back same-role messages (some providers 400), and the
    text enters the TAIL of the history — never the frozen system prompt
    (Invariante #1), so the prefix cache stays warm.
    """
    body = "\n".join(_sanitize_text(t) for t in texts)
    return {"role": "user", "content": f"<system-reminder>\n{body}\n</system-reminder>"}


def _tool_result_message(call: ToolCall, dispatch: ToolDispatch) -> dict:
    """Execute one tool call; wrap its JSON-string result as a role:'tool' message."""
    args = parse_tool_arguments(call.arguments)
    try:
        content = dispatch(call.name, args)
    except Exception as exc:  # the registry never raises, but a custom dispatch might
        content = json.dumps({"error": f"dispatch failed: {type(exc).__name__}: {exc}"})
    return {
        "role": "tool",
        "name": call.name,
        "tool_call_id": call.id,
        "content": content,
    }


def _execute_tool_calls(calls: tuple[ToolCall, ...], dispatch: ToolDispatch) -> list[dict]:
    """Single call -> sequential; multiple -> ThreadPool, results in original order."""
    if len(calls) == 1:
        return [_tool_result_message(calls[0], dispatch)]
    workers = min(_MAX_TOOL_WORKERS, len(calls))
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        results = list(pool.map(lambda call: _tool_result_message(call, dispatch), calls))
    except BaseException:
        # Sinal→exceção (issue #40) com o pool em voo: o __exit__ do executor
        # faria shutdown(wait=True) e JOINARIA os workers vivos — o epílogo só
        # rodaria quando a tool mais LENTA terminasse, e o grace de um
        # supervisor (docker/systemd: ~10s) estoura antes: SIGKILL sem notice
        # nem envelope. wait=False solta o unwinding NA HORA; os workers
        # órfãos morrem com o processo (die_by_signal mata por sinal, sem o
        # join de interpreter-shutdown). Tools nunca levantam por si
        # (_tool_result_message engole Exception) — só BaseException chega cá.
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results


def _sanitize_text(text: str) -> str:
    """Replace lone surrogates that would break json.dumps / API encoding."""
    return _LONE_SURROGATES.sub("�", text)


def _assistant_message(response: NormalizedResponse) -> dict:
    """Build the stored assistant message (spec §2 superset schema).

    ``provider_data`` is preserved so a later turn can replay opaque reasoning
    blobs (e.g. signed thinking blocks) verbatim — several providers 400 without.
    """
    message: dict[str, Any] = {
        "role": "assistant",
        "content": response.content or "",
        "finish_reason": response.finish_reason,
    }
    if response.reasoning:
        message["reasoning"] = response.reasoning
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in response.tool_calls
        ]
    if response.provider_data:
        # Shallow copy: the NormalizedResponse is frozen, but the stored message
        # is mutable history — don't alias the same dict into both.
        message["provider_data"] = dict(response.provider_data)
    return message


def _estimate_tokens(messages: list[dict], system: str) -> int:
    """Cheap char-based token estimate for the compaction threshold (~4 chars/token)."""
    chars = len(system)
    for message in messages:
        content = message.get("content")
        chars += len(content) if isinstance(content, str) else 200
    return chars // 4


def _occupancy(usage: Usage) -> int:
    """How much of the context window this turn is actually using — the provider's
    own count, which replaces the char estimate above once a real one exists.

    ALL FOUR prompt meters, not just ``input_tokens``: since Fatia C the
    transports normalize to the disjoint convention, where ``input_tokens`` is
    only the slice that was NOT served from cache. A cached token occupies the
    window exactly like an uncached one, so reading the uncached slice alone
    inverts the signal (the longer the conversation, the more of it the provider
    caches, the SMALLER the number) and the preflight stops compacting right
    where the history is longest. The output tokens count too: they have just
    been appended to the history the next call will send.
    """
    return (
        (usage.input_tokens or 0)
        + (usage.cache_read_tokens or 0)
        + (usage.cache_write_tokens or 0)
        + (usage.output_tokens or 0)
    )


def _result(
    *,
    final_response: str | None,
    messages: list[dict],
    api_calls: int,
    interrupted: bool,
    error: str | None,
    stop_reason: str | None,
    compacted: bool,
    usage: Usage | None,
    usage_total: Usage | None = None,
    forced_fallback: bool = False,
    error_kind: str | None = None,
    retry_after: float | None = None,
) -> dict:
    # completed: reached a terminal provider stop (stop/length/content_filter)
    # without interrupt or error. "stop_reason is None" covers interrupt,
    # error, and iteration exhaustion — none of which are clean completions.
    completed = stop_reason is not None and not interrupted and error is None
    truncated = stop_reason == "length"
    partial = (truncated or interrupted) and final_response is not None
    return {
        "final_response": final_response,
        "messages": messages,
        "api_calls": api_calls,
        "completed": completed,
        "partial": partial,
        "interrupted": interrupted,
        "error": error,
        "compacted": compacted,
        # Real token usage from the terminal response (None if the provider
        # didn't report any); callers may fall back to an estimate.
        "usage": usage,
        # Field-wise sum over EVERY api call of the turn — the number a cost
        # estimate must use ("usage" alone under-counts a multi-iteration turn).
        "usage_total": usage_total,
        # True if forced tool_choice was requested but the provider ignored it,
        # so the turn fell back to the §5.1 text path (reduced-rigor signal).
        "forced_fallback": forced_fallback,
        # WHAT KIND of failure ``error`` was, when it is one the caller can act
        # on ("quota_exhausted"). The exception object only exists inside the
        # loop; without this the caller sees prose and can't tell a rate limit
        # from a crash. None = unclassified (an ordinary failure).
        "error_kind": error_kind,
        # Seconds the provider asked us to wait, when it said so.
        "retry_after": retry_after,
    }


def run_conversation(
    agent: Agent,
    user_message: str,
    *,
    conversation_history: list[dict] | None = None,
    stream_delta_callback: TextCallback | None = None,
    reasoning_callback: TextCallback | None = None,
    inbox: Inbox | None = None,
    request_overlay: str | None = None,
) -> dict:
    """Run one chat turn to completion. Returns the spec §1 result dict.

    If a streaming callback is provided, the client streams and fires deltas;
    otherwise the call is non-streaming. Either way the loop reads only the
    normalized final response.

    ``request_overlay`` (SUP-05): texto efêmero provider-facing (ex.: notices
    duráveis) incorporado DENTRO da user message do turno — nunca como uma
    user message extra (user/user rejeitado por providers) e nunca no system
    prompt (prefix cache e Invariante #1 intocados). É REAPLICADO sobre a
    lista de messages corrente em CADA API call do turno (sobrevive a
    compactação e a steering) e nunca entra na história do result dict — o
    replay de um turno futuro não o fixa como se fosse fala do usuário.
    """
    if not isinstance(user_message, str):
        raise TypeError(f"user_message must be str, got {type(user_message).__name__}")

    messages: list[dict] = list(conversation_history or [])
    messages.append({"role": "user", "content": _sanitize_text(user_message)})

    overlay = request_overlay.strip() if isinstance(request_overlay, str) else None

    snapshot = agent.system_prompt()
    transport = agent.transport
    max_tokens = agent.resolve_max_tokens()

    api_calls = 0
    final_response: str | None = None
    interrupted = False
    error: str | None = None
    error_kind: str | None = None  # classified failure kind (e.g. quota_exhausted)
    retry_after: float | None = None  # provider's retry-after hint, if any
    stop_reason: str | None = None  # finish_reason of the terminal response, if any
    compacted = False
    compaction_failed = False  # latch por turno: aux quebrado é tentado UMA vez
    forced_fallback = False  # forcing requested but the provider ignored it
    last_usage: Usage | None = None  # token usage of the most recent response
    total_usage: Usage | None = None  # running sum over every call this turn
    prompt_tokens = _estimate_tokens(messages, snapshot.text)
    engine, aux = agent.context_engine, agent.aux_client

    try:
        while api_calls < agent.max_iterations:
            if agent._interrupt_requested:
                interrupted = True
                break

            # Steer: drain any injected texts and append them to the tail before
            # the next LLM call (merged into one user message). Empty -> no-op,
            # so a fresh turn's first iteration never doubles the user prompt.
            if inbox is not None:
                pending = inbox()
                if pending:
                    messages.append(_steer_message(pending))
                    prompt_tokens = _estimate_tokens(messages, snapshot.text)

            # Preflight compaction: if the running history is over the window
            # threshold, summarize the middle before the next API call.
            if (
                engine is not None
                and aux is not None
                and not compaction_failed
                # Resolvida AQUI, dentro do curto-circuito: quem roda sem engine
                # (o `lohra serve`) não paga um stat por iteração, e quem roda
                # com engine reacompanha uma troca de modelo feita pelo
                # `configure` do core entre iterações (issue #38).
                and engine.should_compress(prompt_tokens, agent.resolve_context_window())
            ):
                try:
                    before = prompt_tokens
                    messages = engine.compress(messages, summarize=aux.summarizer())
                    compacted = True
                    prompt_tokens = _estimate_tokens(messages, snapshot.text)
                    # Latch de compactação FÚTIL (issue #38, achado adversarial):
                    # com janela pequena e um bloco grande preso na cauda
                    # protegida, compress roda o summarizer (custo real) mas não
                    # baixa do threshold — e should_compress segue verdadeiro,
                    # re-chamando o aux a cada round-trip até max_iterations. Se
                    # não encolheu, não insiste neste turno (como o latch de
                    # falha); o provider ainda pode recusar por contexto — erro
                    # do turno DELE, visível.
                    if prompt_tokens >= before:
                        compaction_failed = True
                        logger.warning(
                            "preflight compaction did not reduce the prompt "
                            "(%d tokens, window too small for the protected "
                            "tail); not retrying this turn", prompt_tokens)
                except Exception:  # noqa: BLE001 — compactação é DEGRADÁVEL
                    # Latch por TURNO: should_compress segue verdadeiro depois da
                    # falha, então sem isto um aux fora do ar é re-chamado a cada
                    # round-trip (uma chamada HTTP falha por iteração, até
                    # max_iterations, cada uma logando um warning).
                    compaction_failed = True
                    # Um aux quebrado (backend 400, rede) não pode matar o turno
                    # inteiro antes de o agente ver qualquer coisa (épico OBS,
                    # 2026-08-29). Segue sem comprimir; o risco residual é o
                    # provider recusar por contexto — erro do turno DELE, visível.
                    logger.warning("preflight compaction failed; continuing uncompressed",
                                   exc_info=True)

            api_calls += 1
            # Overlay provider-facing (SUP-05): reaplicado do zero sobre a
            # lista ATUAL em cada call — compactação substitui a referência e
            # steering acrescenta mensagens; a cópia garante exatamente UMA
            # ocorrência por chamada e história/result intocados.
            if overlay:
                send_messages = _apply_request_overlay(messages, overlay)
            else:
                send_messages = messages
            # Forced structured output (§5.2): send ONLY the synthetic tool and
            # force it. None -> the normal tools/choice path (byte-identical).
            if agent.forced_tool is not None:
                tools_arg: list[dict] | None = [agent.forced_tool]
                forced_name: str | None = agent.forced_tool["function"]["name"]
            else:
                tools_arg = list(agent.tool_definitions) or None
                forced_name = None
            kwargs = transport.build_kwargs(
                model=agent.model,
                messages=send_messages,
                system=snapshot.text,
                tools=tools_arg,
                max_tokens=max_tokens,
                temperature=agent.temperature,
                tool_choice=forced_name,
                effort=agent.effort,
            )
            try:
                if stream_delta_callback or reasoning_callback:
                    raw = agent.client.stream(
                        on_text=stream_delta_callback,
                        on_reasoning=reasoning_callback,
                        **kwargs,
                    )
                else:
                    raw = agent.client.create(**kwargs)
            except Exception as exc:  # surface the failure; do not fabricate a result
                # Classify BEFORE the exception decays into a string: this is the
                # only frame where the SDK object (status, code, retry-after)
                # still exists.
                error = str(exc)
                error_kind = classify_provider_error(exc)
                retry_after = retry_after_seconds(exc)
                break

            response = transport.normalize_response(raw)
            if response.usage is not None:
                last_usage = response.usage
                total_usage = combine_usage(total_usage, response.usage)
                prompt_tokens = _occupancy(response.usage)
            # A provider or transport may claim a tool-call stop while yielding
            # no executable call (for example, after dropping a partial stream
            # slot). Treat that as a protocol error before appending a dangling
            # assistant turn or constructing a zero-worker executor. Transports
            # canonicalize the legacy ``function_call`` reason to ``tool_calls``
            # before this boundary, so the loop only checks the canonical value.
            if response.finish_reason == "tool_calls" and (
                not response.tool_calls
                or any(not call.id or not call.name for call in response.tool_calls)
            ):
                error = "provider returned incomplete tool_calls"
                break
            messages.append(_assistant_message(response))

            if forced_name is not None:
                # Forced structured output: the synthetic tool's arguments ARE the
                # answer — surface them as the final text (the engine validates).
                forced_args = _forced_call_arguments(response.tool_calls, forced_name)
                if forced_args is not None:
                    final_response = forced_args
                    # Replace the synthetic tool_use turn with a plain assistant
                    # text turn: the leaf's persisted history then has no dangling
                    # tool_use (no tool_result follows), so it stays replay-safe.
                    messages[-1] = {"role": "assistant", "content": forced_args}
                    stop_reason = "stop"
                    break
                # Provider ignored tool_choice → fall back to the §5.1 text path,
                # surfaced (no silent degradation, spec §5.3).
                forced_fallback = True
                logger.info("forced tool_choice ignored by provider; falling back to text")

            if response.finish_reason == "tool_calls" and agent.tool_dispatch:
                # Execute the requested tools, append their results, loop again.
                tool_messages = _execute_tool_calls(response.tool_calls, agent.tool_dispatch)
                messages.extend(tool_messages)
                # A ocupação que o provider reportou é de ANTES destes results:
                # um read_file/web de 80k chars entraria no próximo request sem
                # o preflight saber (review sol #4). Estimativa ~4 chars/token,
                # substituída pelo número real na próxima resposta.
                appended = sum(len(str(m.get("content") or "")) for m in tool_messages)
                prompt_tokens += appended // 4
                continue

            if response.finish_reason == "pause":
                # Provider suspended the turn; resend to continue.
                continue

            # stop / length / content_filter are all terminal. A content_filter
            # refusal is the model's final answer (content may be the refusal text).
            stop_reason = response.finish_reason
            final_response = response.content
            break
        else:
            # Loop fell through without a terminal break — only pause responses,
            # i.e. max_iterations exhausted. Surface it instead of returning a
            # silent empty result indistinguishable from a clean stop.
            if not interrupted and error is None:
                error = (
                    f"max_iterations ({agent.max_iterations}) reached "
                    "without a final response"
                )
    finally:
        # The interrupt request is consumed by the turn it interrupts, so a
        # reused agent starts the next turn clean.
        agent.clear_interrupt()

    return _result(
        final_response=final_response,
        messages=messages,
        api_calls=api_calls,
        interrupted=interrupted,
        error=error,
        stop_reason=stop_reason,
        compacted=compacted,
        usage=last_usage,
        usage_total=total_usage,
        forced_fallback=forced_fallback,
        error_kind=error_kind,
        retry_after=retry_after,
    )
