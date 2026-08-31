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
    # Piso conservador para modelo Claude desconhecido/antigo (era o hardcode
    # global). 1M é a janela da API DIRETA (key-based); a Lohra não tem rota de
    # subscription Anthropic — só Codex — então o número da API vale. Números
    # verificados em platform.claude.com, 2026-08-31.
    default_context_window=200_000,
    model_windows={
        # 1M (janela da API direta, key-based) — platform.claude.com 2026-08-31
        "claude-opus-5": 1_000_000,
        "claude-sonnet-5": 1_000_000,
        "claude-fable-5": 1_000_000,
        "claude-mythos-5": 1_000_000,
        "claude-opus-4-8": 1_000_000,
        "claude-opus-4-7": 1_000_000,
        "claude-opus-4-6": 1_000_000,
        "claude-sonnet-4-6": 1_000_000,
        # 200k — platform.claude.com 2026-08-31
        "claude-opus-4-5": 200_000,
        "claude-sonnet-4-5": 200_000,
        "claude-haiku-4-5": 200_000,
        "claude-3-7": 200_000,
        "claude-3-5": 200_000,
    },
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
    # Família GPT (gpt-4o e gpt-4o-mini, os fallbacks deste perfil): 128k.
    default_context_window=128_000,
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
    # PISO CONSERVADOR, não um palpite: a OpenRouter serve centenas de modelos
    # com janelas de 8k a 1M sob o MESMO perfil, então nenhum número único é
    # verdade. O valor real de cada modelo chega pelo cache do catálogo
    # (``context_length`` do /models → model_windows.json); este piso só existe
    # para o caso "nunca rodei `lohra models`". Errar pra baixo compacta cedo;
    # errar pra cima mata o turno por `length` (issue #38).
    default_context_window=32_000,
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
    # Piso conservador da família (deepseek-chat/reasoner anunciam 64K).
    default_context_window=64_000,
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
    # Piso conservador: o mesmo endpoint serve famílias de 8k a 128k.
    default_context_window=32_000,
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
    # Piso conservador: catálogo aberto e heterogêneo, como o da OpenRouter.
    default_context_window=32_000,
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
    # Piso conservador: os fallbacks (2.0-flash / 1.5-pro) anunciam ≥1M, mas o
    # endpoint OpenAI-compat serve outras famílias — 128k é seguro para todas.
    default_context_window=128_000,
)

# xAI (Grok) — OpenAI-compatible API.
XAI = ProviderProfile(
    name="xai",
    api_mode="chat_completions",
    aliases=("grok",),
    display_name="xAI",
    description="Grok models via xAI's OpenAI-compatible API.",
    signup_url="https://console.x.ai/",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    supports_vision=True,
    # docs.x.ai 2026-08: grok-4/grok-3-mini foram RETIRADOS; 4.6 é o flagship
    # (vision), 4.3 o ativo mais barato confirmado em doc oficial.
    fallback_models=("grok-4.6", "grok-4.3"),
    default_max_tokens=8192,
    # Piso conservador da linha grok-4.x (as docs anunciam mais; não pinamos).
    default_context_window=128_000,
    default_aux_model="grok-4.3",
)

# Zhipu/Z.ai GLM — api.z.ai é o host internacional oficial (docs.z.ai);
# open.bigmodel.cn é o legado/China. ATENÇÃO: o Coding Plan (assinatura) usa
# OUTRO endpoint (/api/coding/paas/v4) e as keys não são intercambiáveis.
GLM = ProviderProfile(
    name="glm",
    api_mode="chat_completions",
    aliases=("zhipu", "zai"),
    display_name="Zhipu GLM",
    description="GLM models via Z.ai's OpenAI-compatible API.",
    signup_url="https://z.ai/",
    env_vars=("ZHIPUAI_API_KEY", "ZAI_API_KEY", "GLM_API_KEY"),
    base_url="https://api.z.ai/api/paas/v4",
    fallback_models=("glm-5.3", "glm-5.3-flash"),
    default_max_tokens=8192,
    # Piso conservador da linha glm-5.x.
    default_context_window=128_000,
    default_aux_model="glm-5.3-flash",
)

# Moonshot (Kimi) — OpenAI-compatible API (.ai is the international endpoint).
KIMI = ProviderProfile(
    name="kimi",
    api_mode="chat_completions",
    aliases=("moonshot",),
    display_name="Moonshot Kimi",
    description="Kimi models via Moonshot's OpenAI-compatible API.",
    signup_url="https://platform.moonshot.ai/",
    env_vars=("MOONSHOT_API_KEY",),
    base_url="https://api.moonshot.ai/v1",
    supports_vision=True,
    # platform.kimi.ai 2026-08: família kimi-k2 original teve EOL (mai/2026) e
    # moonshot-v1-* está em sunset; k3 é o flagship (1M ctx) e k2.6 o value
    # tier — ambos multimodais. Keys .ai e .cn são contas SEPARADAS.
    fallback_models=("kimi-k3", "kimi-k2.6"),
    default_max_tokens=8192,
    # Piso conservador: o comentário acima registra que o k3 anuncia 1M ctx.
    default_context_window=128_000,
    default_aux_model="kimi-k2.6",
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
    # SEM claim (None): o operador escolhe o modelo e o num_ctx do daemon local;
    # qualquer número aqui seria invenção. Quem resolve cai no fallback final.
    default_context_window=None,
)

BUILTIN_PROFILES = (ANTHROPIC, OPENAI, OPENROUTER, DEEPSEEK, GROQ, TOGETHER, GEMINI, XAI, GLM, KIMI, OLLAMA)


def register_builtin_providers() -> None:
    """Register every built-in profile (idempotent: last-writer-wins)."""
    for profile in BUILTIN_PROFILES:
        register_provider(profile)
