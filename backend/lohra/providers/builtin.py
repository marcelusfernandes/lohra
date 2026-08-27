"""Built-in provider profiles.

Purely declarative — client construction, credentials, and streaming live on
the agent (spec §3). Registered by ``lohra.providers`` on import; plugin-based
discovery (user dirs) lands in a later phase.
"""

from __future__ import annotations

from lohra.providers.base import ProviderProfile, register_provider

ANTHROPIC = ProviderProfile(
    name="anthropic",
    api_mode="anthropic_messages",
    aliases=("claude",),
    display_name="Anthropic",
    description="Claude models via the Anthropic Messages API.",
    signup_url="https://console.anthropic.com/",
    env_vars=("ANTHROPIC_API_KEY",),
    base_url="https://api.anthropic.com",
    models_url="https://api.anthropic.com/v1/models",
    supports_vision=True,
    fallback_models=("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"),
    default_max_tokens=16000,
    default_aux_model="claude-haiku-4-5",
)

OPENAI = ProviderProfile(
    name="openai",
    api_mode="chat_completions",
    aliases=("oai",),
    display_name="OpenAI",
    description="OpenAI models via the Chat Completions API.",
    signup_url="https://platform.openai.com/",
    env_vars=("OPENAI_API_KEY",),
    base_url="https://api.openai.com/v1",
    models_url="https://api.openai.com/v1/models",
    supports_vision=True,
    fallback_models=("gpt-4o", "gpt-4o-mini"),
    default_max_tokens=16000,
    default_aux_model="gpt-4o-mini",
)

# OpenAI-compatible providers — same chat_completions transport + client, only
# the base_url and credentials differ. They carry a default_max_tokens because
# the chat_completions transport has no built-in floor (unlike Anthropic's).
OPENROUTER = ProviderProfile(
    name="openrouter",
    api_mode="chat_completions",
    aliases=("or",),
    display_name="OpenRouter",
    description="Many models behind one OpenAI-compatible endpoint.",
    signup_url="https://openrouter.ai/",
    env_vars=("OPENROUTER_API_KEY",),
    base_url="https://openrouter.ai/api/v1",
    models_url="https://openrouter.ai/api/v1/models",
    fallback_models=("openai/gpt-4o-mini",),
    default_max_tokens=8192,
)

DEEPSEEK = ProviderProfile(
    name="deepseek",
    api_mode="chat_completions",
    display_name="DeepSeek",
    description="DeepSeek chat + reasoner via the OpenAI-compatible API.",
    signup_url="https://platform.deepseek.com/",
    env_vars=("DEEPSEEK_API_KEY",),
    base_url="https://api.deepseek.com",
    fallback_models=("deepseek-chat", "deepseek-reasoner"),
    default_max_tokens=8192,
    default_aux_model="deepseek-chat",
)

GROQ = ProviderProfile(
    name="groq",
    api_mode="chat_completions",
    display_name="Groq",
    description="Fast inference via Groq's OpenAI-compatible API.",
    signup_url="https://console.groq.com/",
    env_vars=("GROQ_API_KEY",),
    base_url="https://api.groq.com/openai/v1",
    fallback_models=("llama-3.3-70b-versatile",),
    default_max_tokens=8192,
)

TOGETHER = ProviderProfile(
    name="together",
    api_mode="chat_completions",
    display_name="Together AI",
    description="Open models via Together's OpenAI-compatible API.",
    signup_url="https://api.together.xyz/",
    env_vars=("TOGETHER_API_KEY",),
    base_url="https://api.together.xyz/v1",
    fallback_models=("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
    default_max_tokens=8192,
)

# Google Gemini via its official OpenAI-compatible endpoint (no native transport).
GEMINI = ProviderProfile(
    name="gemini",
    api_mode="chat_completions",
    aliases=("google",),
    display_name="Google Gemini",
    description="Gemini via Google's OpenAI-compatible endpoint.",
    signup_url="https://aistudio.google.com/",
    env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    fallback_models=("gemini-2.0-flash", "gemini-1.5-pro"),
    supports_vision=True,
    default_max_tokens=8192,
)

# Local Ollama — keyless; the OpenAI SDK still needs a placeholder key.
OLLAMA = ProviderProfile(
    name="ollama",
    api_mode="chat_completions",
    display_name="Ollama",
    description="Local models via Ollama's OpenAI-compatible API.",
    env_vars=("OLLAMA_API_KEY",),
    base_url="http://localhost:11434/v1",
    requires_api_key=False,
    default_max_tokens=8192,
)

BUILTIN_PROFILES = (ANTHROPIC, OPENAI, OPENROUTER, DEEPSEEK, GROQ, TOGETHER, GEMINI, OLLAMA)


def register_builtin_providers() -> None:
    """Register every built-in profile (idempotent: last-writer-wins)."""
    for profile in BUILTIN_PROFILES:
        register_provider(profile)
