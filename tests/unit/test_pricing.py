"""Unit tests for :mod:`jarvis.shared.pricing` — rate-table arithmetic.

ADR-0002 Step 3 requires the pricing module to be parametrized over the
real ``data/pricing.json`` table; this test fixture loads the on-disk
table once and asserts that :func:`compute_cost_usd` returns the
arithmetic the spend-attribution canary expects.

The pricing module itself is vendored (``# DO NOT EDIT IN-PLACE`` per
the file-top noqa); tests live here so the L3 cost emitter has a
parametrized contract surface even when the legacy upstream is silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.shared.pricing import compute_cost_usd, load_pricing_table

# Repo-root ``data/pricing.json`` — same path :func:`_pricing_table` in
# ``jarvis.decision.__init__`` consults at runtime.
_PRICING_JSON = Path(__file__).resolve().parents[2] / "data" / "pricing.json"


@pytest.fixture(scope="module")
def pricing_table() -> dict[str, dict[str, float]]:
    """Flattened pricing table from the live ``data/pricing.json``."""
    return load_pricing_table(_PRICING_JSON)


# ---- compute_cost_usd: degenerate inputs ---------------------------------


def test_compute_cost_usd_missing_model_returns_none() -> None:
    """``model=None`` short-circuits to None (no rate lookup)."""
    assert compute_cost_usd(None, 100, 50, 0, 0, {}) is None


def test_compute_cost_usd_missing_tokens_in_returns_none() -> None:
    """``tokens_in=None`` is treated as "no signal" and returns None."""
    table = {"gpt-5.5": {"input": 1, "output": 2}}
    assert compute_cost_usd("gpt-5.5", None, 50, 0, 0, table) is None


def test_compute_cost_usd_missing_tokens_out_returns_none() -> None:
    """``tokens_out=None`` returns None (the output side must be known)."""
    table = {"gpt-5.5": {"input": 1, "output": 2}}
    assert compute_cost_usd("gpt-5.5", 100, None, 0, 0, table) is None


def test_compute_cost_usd_unknown_model_returns_none() -> None:
    """Unknown model returns None (does NOT raise — see ADR-0002 Step 3 hard rule)."""
    assert compute_cost_usd("never-published-model", 100, 50, 0, 0, {}) is None


def test_compute_cost_usd_empty_table_returns_none() -> None:
    """Empty pricing table → None (matches the load_pricing_table fallback path)."""
    assert compute_cost_usd("gpt-5.5", 100, 50, 0, 0, {}) is None


# ---- compute_cost_usd: arithmetic on the real table ----------------------


def test_compute_cost_usd_pure_input_output_no_cache(
    pricing_table: dict[str, dict[str, float]],
) -> None:
    """No cache hits → cost = tokens_in * input_rate / 1M + tokens_out * output_rate / 1M."""
    if "gpt-5.5" not in pricing_table:
        pytest.skip("gpt-5.5 not in pricing.json — table may have been refreshed")
    entry = pricing_table["gpt-5.5"]
    tokens_in = 10_000
    tokens_out = 2_000
    expected = round(
        tokens_in * entry["input"] / 1_000_000
        + tokens_out * entry["output"] / 1_000_000,
        6,
    )
    actual = compute_cost_usd("gpt-5.5", tokens_in, tokens_out, 0, 0, pricing_table)
    assert actual == expected


def test_compute_cost_usd_cache_read_bills_at_cache_rate(
    pricing_table: dict[str, dict[str, float]],
) -> None:
    """Cached input tokens are billed at ``cache_read`` rate, not ``input``."""
    # claude-opus-4-7 ships cache_read_per_1m in the pricing.json fixture.
    if "claude-opus-4-7" not in pricing_table:
        pytest.skip("claude-opus-4-7 not in pricing.json")
    entry = pricing_table["claude-opus-4-7"]
    if "cache_read" not in entry:
        pytest.skip("entry lacks cache_read rate")
    tokens_in = 10_000
    tokens_out = 1_000
    cache_read = 3_000
    non_cached = tokens_in - cache_read
    expected = round(
        non_cached * entry["input"] / 1_000_000
        + tokens_out * entry["output"] / 1_000_000
        + cache_read * entry["cache_read"] / 1_000_000,
        6,
    )
    actual = compute_cost_usd(
        "claude-opus-4-7", tokens_in, tokens_out, cache_read, 0, pricing_table,
    )
    assert actual == expected


def test_compute_cost_usd_cache_write_falls_back_to_input_times_1_25() -> None:
    """When the entry lacks ``cache_write``, fall back to ``input * 1.25``."""
    table = {"model-without-cw": {"input": 1.0, "output": 2.0, "cache_read": 0.1}}
    tokens_in = 10_000
    tokens_out = 1_000
    cache_write = 4_000
    non_cached = tokens_in - cache_write
    expected = round(
        non_cached * 1.0 / 1_000_000
        + tokens_out * 2.0 / 1_000_000
        + cache_write * (1.0 * 1.25) / 1_000_000,
        6,
    )
    actual = compute_cost_usd("model-without-cw", tokens_in, tokens_out, 0, cache_write, table)
    assert actual == expected


def test_compute_cost_usd_cache_write_uses_explicit_rate_when_present(
    pricing_table: dict[str, dict[str, float]],
) -> None:
    """When the entry has ``cache_write``, use it (not the fallback)."""
    if "claude-opus-4-7" not in pricing_table:
        pytest.skip("claude-opus-4-7 not in pricing.json")
    entry = pricing_table["claude-opus-4-7"]
    if "cache_write" not in entry:
        pytest.skip("entry lacks cache_write rate")
    tokens_in = 8_000
    tokens_out = 500
    cache_write = 2_000
    non_cached = tokens_in - cache_write
    expected = round(
        non_cached * entry["input"] / 1_000_000
        + tokens_out * entry["output"] / 1_000_000
        + cache_write * entry["cache_write"] / 1_000_000,
        6,
    )
    actual = compute_cost_usd(
        "claude-opus-4-7", tokens_in, tokens_out, 0, cache_write, pricing_table,
    )
    assert actual == expected


def test_compute_cost_usd_zero_tokens_returns_zero(
    pricing_table: dict[str, dict[str, float]],
) -> None:
    """All-zero token counts → cost == 0.0 (degenerate but well-defined)."""
    if "gpt-5.5" not in pricing_table:
        pytest.skip("gpt-5.5 not in pricing.json")
    actual = compute_cost_usd("gpt-5.5", 0, 0, 0, 0, pricing_table)
    assert actual == 0.0


# ---- load_pricing_table: shape sanity --------------------------------------


def test_load_pricing_table_yields_flattened_legacy_shape() -> None:
    """Each entry in the loaded table has at least ``input`` and ``output`` rates."""
    table = load_pricing_table(_PRICING_JSON)
    assert table, "pricing.json loaded as empty — fixture may be missing"
    for model, entry in table.items():
        assert "input" in entry, f"{model!r} missing 'input' rate"
        assert "output" in entry, f"{model!r} missing 'output' rate"
        # Cache rates are optional; if present they must be floats.
        for opt_key in ("cache_read", "cache_write"):
            if opt_key in entry:
                assert isinstance(entry[opt_key], float)


def test_load_pricing_table_missing_file_returns_empty() -> None:
    """Missing pricing.json with no fallback returns an empty dict (no raise)."""
    table = load_pricing_table(Path("/nonexistent/pricing.json"))
    assert table == {}


def test_load_pricing_table_missing_file_with_fallback() -> None:
    """Missing pricing.json returns the fallback verbatim."""
    fallback = {"model-x": {"input": 0.5, "output": 1.5}}
    table = load_pricing_table(Path("/nonexistent/pricing.json"), fallback_table=fallback)
    assert table == fallback
