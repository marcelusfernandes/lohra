"""Static price table — USD per 1M tokens, standard (short-context) tier.

DATA, not logic: every number was read from the provider's official pricing
page on PRICES_AS_OF (sources below). Prices drift; treat this as a dated
snapshot. An id missing here means estimate_cost returns None (fail-closed) —
never add a guessed price.

Sources (checked 2026-08-28):
- openai:    https://developers.openai.com/api/docs/pricing
- anthropic: https://platform.claude.com/docs/en/about-claude/pricing
- deepseek:  https://api-docs.deepseek.com/quick_start/pricing (peak price;
             off-peak is 50% of it — 01:00-04:00 + 06:00-10:00 UTC Mon-Fri is peak)
- glm:       https://docs.z.ai/guides/overview/pricing (list price; glm-5.3-flash
             has a 50%-off promo until 2026-09-09 not reflected here)
- kimi:      https://platform.kimi.ai/docs/pricing/chat-k3 and .../chat-k26
- xai:       https://docs.x.ai/docs/models (tier <200k context; 200k+ is ~2x)
- gemini:    https://ai.google.dev/gemini-api/docs/pricing (cache STORAGE $/hr
             not modeled; discounted prices in effect today — some double 2027-01-01)
- groq:      https://console.groq.com/docs/models (llama-3.3-70b-versatile moved
             to "Contact Sales" — no public price, hence absent here)
- together:  https://www.together.ai/models/llama-3-3-70b

Notes:
- anthropic cache_write is the 5-minute-TTL rate (1h TTL costs 2x input).
- openai gpt-5.6* long-context (>272K) tiers are ~2x these and not modeled.
- ollama/openrouter are handled by estimate_cost itself (free / dynamic).
"""

from __future__ import annotations

from lohra.pricing.estimate import ModelPrice

PRICES_AS_OF = "2026-08-28"

PRICES: dict[tuple[str, str], ModelPrice] = {
    # --- openai ---
    ("openai", "gpt-4o"): ModelPrice(input_usd=2.50, cached_input_usd=1.25, output_usd=10.00),
    ("openai", "gpt-4o-mini"): ModelPrice(input_usd=0.15, cached_input_usd=0.075, output_usd=0.60),
    ("openai", "gpt-5"): ModelPrice(input_usd=1.25, cached_input_usd=0.125, output_usd=10.00),
    ("openai", "gpt-5-mini"): ModelPrice(input_usd=0.25, cached_input_usd=0.025, output_usd=2.00),
    ("openai", "gpt-5-nano"): ModelPrice(input_usd=0.05, cached_input_usd=0.005, output_usd=0.40),
    ("openai", "gpt-5.2"): ModelPrice(input_usd=1.75, cached_input_usd=0.175, output_usd=14.00),
    ("openai", "gpt-5.4"): ModelPrice(input_usd=2.50, cached_input_usd=0.25, output_usd=15.00),
    ("openai", "gpt-5.5"): ModelPrice(input_usd=5.00, cached_input_usd=0.50, output_usd=30.00),
    ("openai", "gpt-5.3-codex"): ModelPrice(
        input_usd=1.75, cached_input_usd=0.175, output_usd=14.00
    ),
    ("openai", "gpt-5.6-sol"): ModelPrice(
        input_usd=4.00, cached_input_usd=0.40, cache_write_usd=5.00, output_usd=20.00
    ),
    ("openai", "gpt-5.6-terra"): ModelPrice(
        input_usd=2.00, cached_input_usd=0.20, cache_write_usd=2.50, output_usd=12.00
    ),
    ("openai", "gpt-5.6-luna"): ModelPrice(
        input_usd=0.20, cached_input_usd=0.02, cache_write_usd=0.25, output_usd=1.20
    ),
    # --- anthropic (cache_write = 5m TTL) ---
    ("anthropic", "claude-opus-4-8"): ModelPrice(
        input_usd=5.00, cached_input_usd=0.50, cache_write_usd=6.25, output_usd=25.00
    ),
    ("anthropic", "claude-sonnet-4-6"): ModelPrice(
        input_usd=3.00, cached_input_usd=0.30, cache_write_usd=3.75, output_usd=15.00
    ),
    ("anthropic", "claude-haiku-4-5"): ModelPrice(
        input_usd=1.00, cached_input_usd=0.10, cache_write_usd=1.25, output_usd=5.00
    ),
    ("anthropic", "claude-opus-5"): ModelPrice(
        input_usd=5.00, cached_input_usd=0.50, cache_write_usd=6.25, output_usd=25.00
    ),
    ("anthropic", "claude-sonnet-5"): ModelPrice(
        input_usd=2.00, cached_input_usd=0.20, cache_write_usd=2.50, output_usd=10.00
    ),
    ("anthropic", "claude-fable-5"): ModelPrice(
        input_usd=10.00, cached_input_usd=1.00, cache_write_usd=12.50, output_usd=50.00
    ),
    ("anthropic", "claude-mythos-5"): ModelPrice(
        input_usd=10.00, cached_input_usd=1.00, cache_write_usd=12.50, output_usd=50.00
    ),
    # --- deepseek (peak/list price; off-peak = 50%) ---
    ("deepseek", "deepseek-v4-flash"): ModelPrice(
        input_usd=0.44, cached_input_usd=0.014, output_usd=1.32
    ),
    ("deepseek", "deepseek-v4-pro"): ModelPrice(
        input_usd=1.32, cached_input_usd=0.044, output_usd=3.96
    ),
    # --- glm (list price) ---
    ("glm", "glm-5.3"): ModelPrice(input_usd=1.40, cached_input_usd=0.26, output_usd=4.40),
    ("glm", "glm-5.3-flash"): ModelPrice(input_usd=0.15, cached_input_usd=0.03, output_usd=0.50),
    ("glm", "glm-5.2"): ModelPrice(input_usd=1.40, cached_input_usd=0.26, output_usd=4.40),
    ("glm", "glm-5.1"): ModelPrice(input_usd=1.40, cached_input_usd=0.26, output_usd=4.40),
    # --- kimi ---
    ("kimi", "kimi-k3"): ModelPrice(input_usd=3.00, cached_input_usd=0.30, output_usd=15.00),
    ("kimi", "kimi-k2.6"): ModelPrice(input_usd=0.95, cached_input_usd=0.16, output_usd=4.00),
    # --- xai (tier <200k context) ---
    ("xai", "grok-4.6"): ModelPrice(input_usd=2.00, cached_input_usd=0.50, output_usd=6.00),
    ("xai", "grok-4.3"): ModelPrice(input_usd=1.25, cached_input_usd=0.20, output_usd=2.50),
    # --- gemini (live models only; gemini-2.0-flash/1.5-pro were shut down) ---
    ("gemini", "gemini-3.7-flash"): ModelPrice(
        input_usd=0.75, cached_input_usd=0.075, output_usd=3.75
    ),
    ("gemini", "gemini-3.6-flash"): ModelPrice(
        input_usd=0.75, cached_input_usd=0.075, output_usd=3.75
    ),
    ("gemini", "gemini-3.5-flash"): ModelPrice(
        input_usd=1.50, cached_input_usd=0.15, output_usd=9.00
    ),
    ("gemini", "gemini-3.5-flash-lite"): ModelPrice(
        input_usd=0.30, cached_input_usd=0.03, output_usd=2.50
    ),
    ("gemini", "gemini-3.1-pro-preview"): ModelPrice(
        input_usd=2.00, cached_input_usd=0.20, output_usd=12.00
    ),
    ("gemini", "gemini-3-flash-preview"): ModelPrice(
        input_usd=0.50, cached_input_usd=0.05, output_usd=3.00
    ),
    ("gemini", "gemini-2.5-pro"): ModelPrice(
        input_usd=1.25, cached_input_usd=0.125, output_usd=10.00
    ),
    ("gemini", "gemini-2.5-flash"): ModelPrice(
        input_usd=0.30, cached_input_usd=0.03, output_usd=2.50
    ),
    # --- groq (publicly priced models only) ---
    ("groq", "openai/gpt-oss-120b"): ModelPrice(input_usd=0.15, output_usd=0.60),
    ("groq", "openai/gpt-oss-20b"): ModelPrice(input_usd=0.075, output_usd=0.30),
    ("groq", "qwen/qwen3.6-27b"): ModelPrice(input_usd=0.60, output_usd=3.00),
    ("groq", "qwen/qwen3.8-27b"): ModelPrice(input_usd=0.80, output_usd=4.00),
    # --- together ---
    ("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"): ModelPrice(
        input_usd=1.04, output_usd=1.04
    ),
}

# Subscription models priced by their API twin — labeled basis="api_equivalent"
# by estimate_cost (the real bill is the plan's flat fee, not per-token).
EQUIVALENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("openai-codex", "gpt-5.6-sol"): ("openai", "gpt-5.6-sol"),
    ("openai-codex", "gpt-5.6-terra"): ("openai", "gpt-5.6-terra"),
    ("openai-codex", "gpt-5.6-luna"): ("openai", "gpt-5.6-luna"),
    ("openai-codex", "gpt-5.5"): ("openai", "gpt-5.5"),
    ("openai-codex", "gpt-5.3-codex"): ("openai", "gpt-5.3-codex"),
}
