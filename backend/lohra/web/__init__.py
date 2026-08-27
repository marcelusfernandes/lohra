"""Browser tools — ``web_fetch`` (read a page) and ``web_search`` (find pages).

These are plain, stateless tools (like fs/terminal): they need only httpx, not a
provider client, so they self-register with real handlers and work everywhere —
the main agent, subagents, and the server (gated there by the tool allow-list).

SSRF is handled in the fetch layer (``safety.validate_public_url``), not at the
tool boundary, so every caller is protected by construction: the URL is
model-chosen and the model is influenced by the very content it fetches, so a
prompt-injected ``http://169.254.169.254/...`` must be refused regardless of who
calls. Redirects are followed manually so each hop is re-validated.
"""
