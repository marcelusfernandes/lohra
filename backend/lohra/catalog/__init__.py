"""Model catalog — which models are actually reachable, per provider.

``build_catalog`` is the pure read (live fetch where a key exists, the native
Ollama probe, the subscription model); ``ListModelsTool`` is the agent-facing,
bounded view of it. Neither builds a model client, and neither raises once
the HTTP client exists (creating it is the seam the test suite blocks).
"""

# ``default_http_client`` is deliberately NOT re-exported: the test suite
# neutralizes that one name on ``lohra.catalog.catalog``, and a second binding
# here would be a route to the real network that the guard cannot see.
from lohra.catalog.catalog import Catalog, ProviderModels, build_catalog
from lohra.catalog.tool import ListModelsTool, register_list_models_tool_schema

__all__ = [
    "Catalog",
    "ProviderModels",
    "build_catalog",
    "ListModelsTool",
    "register_list_models_tool_schema",
]
