"""OpenAI/Codex subscription auth (Fase 10) — OPT-IN, ToS-gray.

Lets a user with a ChatGPT/Codex subscription drive Lohra with it instead of a
paid API key, by REUSING an existing Codex CLI login (Lohra never re-implements
the OAuth browser flow). ⚠️ Using a consumer subscription in a third-party agent
likely violates OpenAI's ToS (account-ban risk) — so this is default-OFF and
gated behind an explicit acknowledgement. Provider-specific constants are PUBLIC
facts from the open-source openai/codex repo (cited in constants.py), not guesses.
"""
