"""Human rendering of tokens + cost (Fatia C) — one phrasing everywhere."""

from lohra.agent.types import Usage
from lohra.pricing.estimate import CostEstimate
from lohra.pricing.render import cost_line, format_cost, format_tokens


def test_format_tokens_without_cache_has_no_parenthetical():
    assert format_tokens(Usage(input_tokens=1200, output_tokens=340)) == "1,200 in + 340 out"


def test_format_tokens_shows_cache_read():
    usage = Usage(input_tokens=12_300, output_tokens=1_200, cache_read_tokens=9_800)
    assert format_tokens(usage) == "12,300 in (9,800 cached) + 1,200 out"


def test_format_tokens_shows_cache_write_too():
    usage = Usage(input_tokens=100, output_tokens=5, cache_read_tokens=20, cache_write_tokens=30)
    assert format_tokens(usage) == "100 in (20 cached, 30 written) + 5 out"


def test_format_tokens_none_usage_says_so():
    assert format_tokens(None) == "no usage reported"


def test_format_cost_reports_gross_and_saving():
    line = format_cost(
        CostEstimate(usd=0.01, gross_usd=0.04, basis="api_list_price", source="snapshot X")
    )
    assert "$0.010000" in line and "gross $0.040000" in line and "saved $0.030000" in line
    assert line.endswith("snapshot X")


def test_format_cost_omits_a_zero_saving():
    line = format_cost(CostEstimate(usd=0.01, gross_usd=0.01, source="snapshot X"))
    assert "saved" not in line and "gross" not in line


def test_format_cost_calls_a_write_premium_what_it_is():
    line = format_cost(CostEstimate(usd=0.05, gross_usd=0.04, source="snapshot X"))
    assert "cache write premium $0.010000" in line
    assert "saved" not in line  # never dressed up as a discount


def test_format_cost_shows_a_non_list_basis():
    line = format_cost(CostEstimate(usd=1.0, gross_usd=1.0, basis="api_equivalent"))
    assert "[api_equivalent]" in line


def test_format_cost_none_is_none():
    assert format_cost(None) is None


def test_cost_line_joins_money_and_tokens():
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
    line = cost_line(usage, provider="openai", model="gpt-4o")
    assert line.startswith("cost: $")
    assert "1,000,000 in (1,000,000 cached) + 0 out" in line


def test_cost_line_none_when_unpriced():
    usage = Usage(input_tokens=10)
    assert cost_line(usage, provider="openai", model="mystery-9000") is None


def test_cost_line_none_without_usage_or_model():
    assert cost_line(None, provider="openai", model="gpt-4o") is None
    assert cost_line(Usage(input_tokens=1), provider=None, model="gpt-4o") is None
    assert cost_line(Usage(input_tokens=1), provider="openai", model=None) is None


def test_format_cost_carries_the_note_about_a_missing_cache_price():
    line = format_cost(
        CostEstimate(usd=1.0, gross_usd=1.0, source="snapshot X", note="no cache read price")
    )
    assert line.endswith("no cache read price")
