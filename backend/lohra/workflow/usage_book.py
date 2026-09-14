"""Immutable five-meter bookkeeping; only input/output debit a token budget."""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

from lohra.agent.types import Usage, combine_usage

FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def maximum(a: Usage, b: Usage) -> Usage:
    return Usage(**{name: max(0, getattr(a, name), getattr(b, name)) for name in FIELDS})


def total(values) -> Usage:
    result = Usage()
    for usage in values:
        result = combine_usage(result, usage)
    return result


@dataclass(frozen=True)
class TokenState:
    """The debit and its execution marker are committed by one reference swap.

    A fresh dictionary is prepared before that swap and never mutated afterwards.
    Neither a failed preparation nor an exception after returning can leave a
    debit without its applied marker. Lifetime reservations are independent.
    """

    tokens_in: int = 0
    tokens_out: int = 0
    charges: int = 0
    applied: Mapping[str, Usage] = field(default_factory=lambda: MappingProxyType({}))

    def apply(self, sub_id: str, usage: Usage) -> "TokenState":
        previous = self.applied.get(sub_id, Usage())
        current = maximum(previous, usage)
        if current == previous:
            return self
        measured = previous.input_tokens > 0 or previous.output_tokens > 0
        positive = current.input_tokens > 0 or current.output_tokens > 0
        return TokenState(
            self.tokens_in + current.input_tokens - previous.input_tokens,
            self.tokens_out + current.output_tokens - previous.output_tokens,
            self.charges + int(positive and not measured),
            MappingProxyType({**self.applied, sub_id: current}),
        )
