"""The subscription provider profile + client builder (Fase 10, B3).

When subscription mode is active, the CLI swaps in this provider (api_mode
"responses", Codex base_url) and a ResponsesClient bound to the resolved bearer.
OpenAI only. The default model slug is the research's best guess and is fully
overridable with --model (the exact slug the Codex backend accepts is a
KNOWN-unknown, validated live with the user's subscription).
"""

from __future__ import annotations

from pathlib import Path

from lohra.agent.client import ResponsesClient
from lohra.providers.base import ProviderProfile
from lohra.subscription.codex_creds import read_codex_model
from lohra.subscription.credentials import SubscriptionError, resolve

# Fallback model when the Codex config doesn't name one. gpt-5.5 verified live
# against a ChatGPT-account Codex backend (gpt-5-codex etc. are rejected there).
DEFAULT_CODEX_MODEL = "gpt-5.5"


def codex_default_model() -> str:
    """The user's Codex-configured model, else the verified fallback."""
    return read_codex_model() or DEFAULT_CODEX_MODEL

CODEX_PROVIDER = ProviderProfile(
    name="openai-codex",
    api_mode="responses",
    display_name="OpenAI (Codex subscription)",
    description="Use a ChatGPT/Codex subscription via an existing Codex CLI login (opt-in, ToS-gray).",
    auth_type="oauth_external",
    requires_api_key=False,
    fallback_models=(DEFAULT_CODEX_MODEL,),
    # gpt-5.5 tem 1M na API pura, mas o backend Codex/subscription (a rota que a
    # Lohra usa) CAPA em 400k total (272k in + 128k out) — verificado em issues do
    # openai/codex e na doc, 2026-08-31. Usar 1M aqui mataria o turno por `length`.
    # Toda a família gpt-5* servida pelo Codex = 400k, então um piso único basta
    # (sem model_windows aqui).
    default_context_window=400_000,
    # Empty → the aux client (compaction/titles) falls back to the RESOLVED model
    # (cli.py: default_aux_model or chosen_model). A Codex account accepts only the
    # one validated slug; pinning gpt-5.5 here would 400 for accounts on another.
    default_aux_model="",
)


def build_subscription_client(home: Path, *, now: float | None = None) -> ResponsesClient:
    """A ResponsesClient bound to the user's Codex subscription token. Raises
    SubscriptionError (token-free) if mode is off or the login is unusable."""
    creds = resolve(home, now=now)
    if creds is None:
        raise SubscriptionError("subscription mode is not active")
    return ResponsesClient(
        api_key=creds.token, base_url=creds.base_url, default_headers=creds.headers
    )
