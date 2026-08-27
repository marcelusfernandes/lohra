"""Onboarding messages — static text, no I/O, no detection (ONB-1).

An error that stops at "it doesn't work" makes the user go read the source. The
repo's exemplar (``workflow/strategies.py`` tier warning) names the exact file
and the exact effect; these messages copy that form: every path out is a command
the user can paste.
"""

from __future__ import annotations

# The three ways into a working Lohra. Two of them need NO API key at all, and
# the old message mentioned neither — that omission is the ONB-1 bug.
#
# Keep the first line starting with "no provider configured": callers and tests
# key on it.
NO_PROVIDER_CONFIGURED = """no provider configured — there are three ways in:

  1. API key (paid, any provider)
       export ANTHROPIC_API_KEY=sk-...      # or OPENAI_API_KEY, OPENROUTER_API_KEY,
                                            # DEEPSEEK_API_KEY, GROQ_API_KEY,
                                            # TOGETHER_API_KEY, GEMINI_API_KEY
     Keys are also read from ~/.lohra/.env (one KEY=value per line), which is
     what the desktop app writes and what a Finder-launched app relies on.

  2. OpenAI/Codex subscription (no API key; opt-in, ToS-gray)
       lohra auth enable        # prints the ToS warning and asks for a yes
       lohra auth login         # Lohra's own login (auto-refreshing)

  3. Local models, keyless (nothing leaves the machine)
       ollama serve             # then, in another shell:
       lohra chat "oi" --provider ollama

Or name a provider explicitly with --provider <name>.

Not sure which? Run `lohra init` — it detects what this machine already has and
sets up the rest (it is read-only without a terminal, so it is safe in CI), or
`lohra doctor` for a read-only report with the exact command for each gap."""
