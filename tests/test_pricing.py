"""Tests for memory.pricing.compute_cost_usd."""

import pytest

from memory.pricing import compute_cost_usd

# ---------------------------------------------------------------------------
# Shared fixture — small inline table; tests must not load config.yaml
# ---------------------------------------------------------------------------

PRICING: dict = {
    "grok-4.20": {"input": 3.00, "output": 15.00, "cache_read": 0.30},
    # claude-opus has an explicit cache_write rate
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_write": 18.75,
    },
    # model without cache_write key — fallback to input * 1.25
    "grok-reasoning": {"input": 5.00, "output": 20.00, "cache_read": 0.50},
}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _cost(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read_in: int | None = None,
    cache_write_in: int | None = None,
    table: dict = PRICING,
) -> float | None:
    return compute_cost_usd(model, tokens_in, tokens_out, cache_read_in, cache_write_in, table)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestKnownModel:
    def test_no_cache(self) -> None:
        """1 000 prompt + 500 completion, no cache activity."""
        # (1000 * 3.00 + 500 * 15.00) / 1_000_000
        expected = round((1000 * 3.00 + 500 * 15.00) / 1_000_000, 6)
        assert _cost("grok-4.20", 1000, 500) == expected

    def test_cache_read_reduces_input_cost(self) -> None:
        """600 of 1 000 prompt tokens served from cache."""
        non_cached = 1000 - 600
        # non_cached * input_rate + 600 * cache_read_rate + 500 * output_rate
        expected = round(
            (non_cached * 3.00 + 600 * 0.30 + 500 * 15.00) / 1_000_000,
            6,
        )
        assert _cost("grok-4.20", 1000, 500, cache_read_in=600) == expected

    def test_result_rounded_to_6dp(self) -> None:
        result = _cost("grok-4.20", 1, 1)
        assert result is not None
        assert result == round(result, 6)


class TestUnknownModel:
    def test_returns_none(self) -> None:
        assert _cost("frobnicator", 1000, 500) is None

    def test_warns_once(self, caplog: pytest.LogCaptureFixture) -> None:
        """Second call with same unknown model must not produce a second warning."""
        import logging
        from memory import pricing as pricing_mod

        # Ensure the unknown model hasn't been warned yet in this test's scope
        pricing_mod._warned_models.discard("frobnicator-warn-test")

        with caplog.at_level(logging.WARNING, logger="memory.pricing"):
            _cost("frobnicator-warn-test", 100, 50)
            _cost("frobnicator-warn-test", 100, 50)

        warnings = [r for r in caplog.records if "frobnicator-warn-test" in r.message]
        assert len(warnings) == 1


class TestMissingTokens:
    def test_none_model(self) -> None:
        assert _cost(None, 1000, 500) is None

    def test_none_tokens_in(self) -> None:
        assert _cost("grok-4.20", None, 500) is None

    def test_none_tokens_out(self) -> None:
        assert _cost("grok-4.20", 1000, None) is None

    def test_empty_model_string(self) -> None:
        assert _cost("", 1000, 500) is None


class TestCacheWrite:
    def test_fallback_rate_is_input_times_1_25(self) -> None:
        """Model without explicit cache_write uses input * 1.25."""
        # grok-reasoning has no cache_write key
        cache_write = 200
        tokens_in = 1000
        tokens_out = 300
        entry = PRICING["grok-reasoning"]
        non_cached = tokens_in - cache_write
        fallback_rate = entry["input"] * 1.25
        expected = round(
            (non_cached * entry["input"] + tokens_out * entry["output"] + cache_write * fallback_rate)
            / 1_000_000,
            6,
        )
        result = _cost("grok-reasoning", tokens_in, tokens_out, cache_write_in=cache_write)
        assert result == expected

    def test_explicit_cache_write_rate_honored(self) -> None:
        """Model with explicit cache_write key uses that rate, not input * 1.25."""
        cache_write = 400
        tokens_in = 1000
        tokens_out = 200
        entry = PRICING["claude-opus-4-7"]
        non_cached = tokens_in - cache_write
        expected = round(
            (
                non_cached * entry["input"]
                + tokens_out * entry["output"]
                + cache_write * entry["cache_write"]
            )
            / 1_000_000,
            6,
        )
        result = _cost("claude-opus-4-7", tokens_in, tokens_out, cache_write_in=cache_write)
        assert result == expected
