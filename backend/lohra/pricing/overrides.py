"""Operator price overrides — ``~/.lohra/pricing.json`` beats the snapshot.

The shipped table (``table.py``) is a DATED snapshot of published list prices.
Prices drift, private rates exist, and a model can ship before the snapshot is
refreshed — so the operator gets the last word, per model, in a file the agent
never writes. An override wins over the snapshot and may price a model (or a
provider) the snapshot does not know at all.

Shape (nested provider -> model -> price, USD per 1M tokens)::

    {
      "openai":     {"gpt-9": {"input_usd": 1.25, "output_usd": 10.0,
                               "cached_input_usd": 0.125, "cache_write_usd": 1.5}},
      "openrouter": {"openai/gpt-4o": {"input_usd": 2.5, "output_usd": 10.0}}
    }

Nested rather than a flat ``"provider/model"`` key precisely because model ids
already contain slashes (openrouter). ``input_usd`` and ``output_usd`` are
required; the two cache rates are optional and fall back to the input rate WITH
a note (see ``estimate``) — a missing rate is never an invented discount.

Fail-safe like ``load_tiers``: an absent, unreadable or malformed file means "no
override", never an exception. A single bad entry is dropped; the rest load.
"""

from __future__ import annotations

import json
from pathlib import Path

from lohra.pricing.estimate import OVERRIDE_SOURCE, ModelPrice

PRICING_FILE = "pricing.json"

__all__ = ["OVERRIDE_SOURCE", "PRICING_FILE", "load_price_overrides", "price_overrides_path"]

_REQUIRED = ("input_usd", "output_usd")
_OPTIONAL = ("cached_input_usd", "cache_write_usd")


def price_overrides_path(home: Path) -> Path:
    """Where the operator's price file lives under an (already resolved) home."""
    return home / PRICING_FILE


def _rate(value: object) -> float | None:
    """A non-negative number, or None. ``True`` is an int in Python and is not a
    price — a bool here means the file is wrong, not that the model is free."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value >= 0 else None


def _as_price(entry: object) -> ModelPrice | None:
    if not isinstance(entry, dict):
        return None
    required = [_rate(entry.get(name)) for name in _REQUIRED]
    if any(rate is None for rate in required):
        return None
    optional = {name: _rate(entry.get(name)) for name in _OPTIONAL}
    return ModelPrice(input_usd=required[0], output_usd=required[1], **optional)


def load_price_overrides(path: Path) -> dict[tuple[str, str], ModelPrice]:
    """Load the operator's overrides; ``{}`` if the file is absent or unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    resolved: dict[tuple[str, str], ModelPrice] = {}
    for provider, models in data.items():
        if not provider or not isinstance(models, dict):
            continue
        for model, entry in models.items():
            price = _as_price(entry)
            if model and price is not None:
                resolved[(str(provider), str(model))] = price
    return resolved
