"""OpenAI inclusive usage plus an explicit, canonical known-floor annotation."""
from typing import Any


def usage_fields(observed: dict | None, complete: bool) -> dict[str, Any]:
    if complete and observed is not None:
        return {"usage": observed}
    annotation: dict[str, Any] = {"status": "unknown"}
    if observed is not None:
        prompt = observed.get("prompt_tokens_details") or {}
        completion = observed.get("completion_tokens_details") or {}
        cached, written = prompt.get("cached_tokens", 0), prompt.get("cache_write_tokens", 0)
        annotation = {"status": "lower_bound", "observed": {
            "input_tokens": observed["prompt_tokens"] - cached - written,
            "output_tokens": observed["completion_tokens"],
            "cache_read_tokens": cached, "cache_write_tokens": written,
            "reasoning_tokens": completion.get("reasoning_tokens", 0),
        }}
    return {"usage": None, "lohra_usage": annotation}
