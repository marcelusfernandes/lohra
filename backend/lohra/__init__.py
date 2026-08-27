"""Lohra — self-improving AI agent with a desktop app.

Backend package: agent core, provider abstraction, tool system, memory/skills,
and the FastAPI gateway. See docs/ARCHITECTURE.md for the full design.
"""

try:  # fonte única: o metadata do pacote instalado (pyproject) — nunca diverge
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("lohra")
except Exception:  # checkout sem install (ex.: leitura direta do source)
    __version__ = "0.0.0+unknown"
