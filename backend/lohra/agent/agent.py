"""Agent — the live session controller.

Unlike the canonical response types (frozen, immutable), the Agent is a
stateful session object: it caches the frozen system prompt (Invariante #1),
holds the interrupt flag, and resolves per-call configuration. The spec models
this same mutable state (``agent._cached_system_prompt``,
``agent._interrupt_requested``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from lohra.agent.aux import AuxClient
from lohra.agent.client import ModelClient
from lohra.agent.context import ContextEngine
from lohra.agent.system_prompt import SystemPromptSnapshot, build_system_prompt
from lohra.memory.store import MemoryStore
from lohra.providers.base import ProviderProfile
from lohra.skills.store import SkillStore
from lohra.providers.transports.base import Transport, get_transport

DEFAULT_MAX_ITERATIONS = 8

# Fallback FINAL da janela de contexto: só chega aqui quem não tem override, não
# está no cache do catálogo e cujo perfil não faz claim (ollama). Era o hardcode
# global até a issue #38 — mantê-lo aqui garante que ninguém que já estava certo
# regrida.
DEFAULT_CONTEXT_WINDOW = 200_000

# A tool executor: (name, parsed args) -> JSON-string result envelope.
ToolDispatch = Callable[[str, dict[str, Any]], str]


@dataclass
class Agent:
    """A conversation session bound to one provider/model/client."""

    model: str
    provider: ProviderProfile
    client: ModelClient

    # system-prompt inputs (frozen into the snapshot on first use)
    identity: str | None = None  # SOUL.md persona; None -> built-in identity
    system_message: str | None = None
    context_files: tuple[tuple[str, str], ...] = ()
    environment_hints: Mapping[str, str] = field(default_factory=dict)
    # memory + skills snapshots are captured once and frozen into the prompt
    # (Invariante #1)
    memory_store: MemoryStore | None = None
    skill_store: SkillStore | None = None

    # tools: definitions sent to the model + an executor for tool_use turns.
    # Both unset = chat-only (Phase 1 behavior).
    tool_definitions: tuple[dict, ...] = ()
    tool_dispatch: ToolDispatch | None = None

    # forced structured output (workflow §5.2): when set to a tool def, the turn
    # sends ONLY that tool and forces tool_choice; the tool's arguments ARE the
    # answer. None (default) -> byte-identical normal behavior. Set per-leaf via
    # the core.spawn `configure` hook; never on a frozen prompt (rides in tools=).
    forced_tool: dict | None = None

    # loop configuration
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_tokens: int | None = None
    temperature: float | None = None
    # reasoning effort (provider-specific; emitted only where supported — OpenAI/
    # Responses). None → unchanged. Mutable per spawn like ``model``.
    effort: str | None = None

    # context compression (both set = preflight compaction enabled)
    # None = "resolva" (o caminho normal); um int é um override EXPLÍCITO do
    # chamador e ganha de tudo. Ver ``resolve_context_window``.
    context_window: int | None = None
    context_engine: ContextEngine | None = None
    aux_client: AuxClient | None = None

    # mutable session state (never part of construction)
    _cached_system_prompt: SystemPromptSnapshot | None = field(
        default=None, init=False, repr=False
    )
    _interrupt_requested: bool = field(default=False, init=False, repr=False)

    @property
    def transport(self) -> Transport:
        """The transport for this provider's api_mode; fail fast if unregistered."""
        transport = get_transport(self.provider.api_mode)
        if transport is None:
            raise LookupError(
                f"no transport registered for api_mode {self.provider.api_mode!r} "
                f"(provider {self.provider.name!r})"
            )
        return transport

    def system_prompt(self) -> SystemPromptSnapshot:
        """Restore-or-build the frozen snapshot (built once, then reused).

        The memory snapshot is captured here on first use and frozen into the
        prompt; mid-session memory writes hit disk but never this prompt.
        """
        if self._cached_system_prompt is None:
            memory_snapshot, user_profile = "", ""
            if self.memory_store is not None:
                snapshot = self.memory_store.snapshot()
                memory_snapshot, user_profile = snapshot["memory"], snapshot["user"]
            skills_index = self.skill_store.snapshot() if self.skill_store is not None else ""
            self._cached_system_prompt = build_system_prompt(
                identity=self.identity,
                system_message=self.system_message,
                context_files=self.context_files,
                environment_hints=self.environment_hints,
                memory_snapshot=memory_snapshot,
                user_profile=user_profile,
                skills_index=skills_index,
            )
        return self._cached_system_prompt

    def resolve_max_tokens(self) -> int | None:
        """Explicit override wins; otherwise the provider profile owns the default."""
        if self.max_tokens is not None:
            return self.max_tokens
        return self.provider.get_max_tokens(self.model)

    def resolve_context_window(self) -> int:
        """A janela de contexto a assumir AGORA, na ordem: override explícito >
        cache do catálogo > piso do perfil > ``DEFAULT_CONTEXT_WINDOW``.

        Chamada a cada decisão de compactação, de propósito, e NÃO memoizada no
        agente: o hook ``configure`` do OrchestrationCore troca ``self.model``
        (e até ``self.provider``) por sub-sessão, e uma janela congelada na
        construção descreveria o modelo errado. O custo por chamada é um ``stat``
        — o parse do cache é memoizado dentro de ``catalog.windows``.

        "Não sei" degrada para o valor mais CONSERVADOR disponível, nunca para o
        mais otimista: errar pra baixo compacta cedo, errar pra cima mata o turno
        por ``length`` sem defesa (issue #38).
        """
        if self.context_window is not None:
            return self.context_window
        # Importado aqui e não no topo: só quem realmente compacta paga o custo
        # de importar o pacote ``catalog`` (o ``__init__`` dele puxa a tool e os
        # tiers), e nada disso entra na construção de um Agent. Não há ciclo —
        # ``catalog.catalog`` importa ``agent.client``, não este módulo — e o
        # próprio ``windows`` só depende de stdlib + paths + safeio.
        from lohra.catalog import windows

        known = windows.lookup(self.provider.name, self.model)
        if known:
            return known
        return self.provider.get_context_window(self.model) or DEFAULT_CONTEXT_WINDOW

    def request_interrupt(self) -> None:
        """Signal the loop to stop at the next safe boundary."""
        self._interrupt_requested = True

    def clear_interrupt(self) -> None:
        self._interrupt_requested = False
