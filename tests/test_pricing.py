"""Tests for memory.pricing.compute_cost_usd + load_pricing_table."""

import json
from pathlib import Path

import pytest

from memory.cold.pricing import compute_cost_usd, load_pricing_table

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
        from memory.cold import pricing as pricing_mod

        # Ensure the unknown model hasn't been warned yet in this test's scope
        pricing_mod._warned_models.discard("frobnicator-warn-test")

        with caplog.at_level(logging.WARNING, logger="memory.cold.pricing"):
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


# ---------------------------------------------------------------------------
# load_pricing_table
# ---------------------------------------------------------------------------

class TestLoadPricingTable:
    def _write(self, tmp_path: Path, payload: dict) -> Path:
        path = tmp_path / "pricing.json"
        path.write_text(json.dumps(payload))
        return path

    def test_flattens_llm_section(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {
            "llm": {
                "grok-4.20": {
                    "input_per_1m": 2.0,
                    "output_per_1m": 6.0,
                    "cache_read_per_1m": 0.2,
                },
                "claude-opus-4-7": {
                    "input_per_1m": 5.0,
                    "output_per_1m": 25.0,
                    "cache_read_per_1m": 0.5,
                    "cache_write_per_1m": 6.25,
                },
            },
            "tts": {"speech-02-turbo": {"unit": "character"}},  # must be ignored
        })
        table = load_pricing_table(path)
        assert table["grok-4.20"] == {"input": 2.0, "output": 6.0, "cache_read": 0.2}
        assert table["claude-opus-4-7"] == {
            "input": 5.0,
            "output": 25.0,
            "cache_read": 0.5,
            "cache_write": 6.25,
        }
        assert "speech-02-turbo" not in table

    def test_round_trip_with_compute_cost_usd(self, tmp_path: Path) -> None:
        """Loaded table must plug straight into compute_cost_usd."""
        path = self._write(tmp_path, {
            "llm": {
                "grok-4.20": {
                    "input_per_1m": 2.0,
                    "output_per_1m": 6.0,
                    "cache_read_per_1m": 0.2,
                },
            },
        })
        table = load_pricing_table(path)
        # 1000 input, 500 output, no cache
        expected = round((1000 * 2.0 + 500 * 6.0) / 1_000_000, 6)
        assert compute_cost_usd("grok-4.20", 1000, 500, None, None, table) == expected

    def test_missing_input_or_output_skipped(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {
            "llm": {
                "bad": {"input_per_1m": None, "output_per_1m": 1.0},
                "also-bad": {"input_per_1m": 1.0, "output_per_1m": None},
                "good": {"input_per_1m": 1.0, "output_per_1m": 1.0},
            },
        })
        table = load_pricing_table(path)
        assert "bad" not in table
        assert "also-bad" not in table
        assert "good" in table

    def test_missing_file_uses_fallback(self, tmp_path: Path) -> None:
        fb = {"some-model": {"input": 1.0, "output": 2.0}}
        table = load_pricing_table(tmp_path / "nonexistent.json", fallback_table=fb)
        assert table == fb

    def test_missing_file_no_fallback_returns_empty(self, tmp_path: Path) -> None:
        assert load_pricing_table(tmp_path / "nonexistent.json") == {}

    def test_malformed_json_uses_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.json"
        path.write_text("not{valid}json")
        fb = {"m": {"input": 1.0, "output": 1.0}}
        assert load_pricing_table(path, fallback_table=fb) == fb

    def test_empty_llm_section(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"llm": {}})
        assert load_pricing_table(path) == {}
