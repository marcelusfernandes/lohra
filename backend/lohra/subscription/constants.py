"""Public Codex constants — sourced from the open-source openai/codex repo (cited).

These are used ONLY when the user explicitly opts into subscription auth; they are
never a default. Citations are to openai/codex (codex-rs/, branch main) so the
values are verifiable facts, not fabrications.
"""

from __future__ import annotations

# Subscription requests go here and speak the RESPONSES API only (not chat).
# codex-rs/model-provider-info/src/lib.rs:38 (CHATGPT_CODEX_BASE_URL)
CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# OAuth refresh endpoint — codex-rs/login/src/auth/manager.rs:186 (REFRESH_TOKEN_URL)
REFRESH_URL = "https://auth.openai.com/oauth/token"

# OAuth client id — codex-rs/login/src/auth/manager.rs:1444 (pub const CLIENT_ID)
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

# Sent on every request — codex-rs/login/src/auth/default_client.rs:41 (DEFAULT_ORIGINATOR)
ORIGINATOR = "codex_cli_rs"

# Header names — codex-rs/model-provider/src/bearer_auth_provider.rs
ACCOUNT_ID_HEADER = "ChatGPT-Account-ID"
ORIGINATOR_HEADER = "originator"
