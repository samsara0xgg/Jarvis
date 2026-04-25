"""LLM cost calculator for trace instrumentation.

Converts token counts from a single LLM turn into a USD cost using a
pricing table loaded from ``data/pricing.json`` (refreshed via
``scripts/refresh_pricing.py`` from LiteLLM's community pricing snapshot).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Module-level set so each unknown model is warned exactly once per process.
_warned_models: set[str] = set()

DEFAULT_PRICING_JSON = Path("data/pricing.json")


def compute_cost_usd(
    model: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cache_read_in: int | None,
    cache_write_in: int | None,
    pricing_table: dict,
) -> float | None:
    """Compute USD cost for one LLM turn.

    Args:
        model: Model identifier matching a key in ``pricing_table``.
        tokens_in: Total prompt tokens (including any cache hits).
        tokens_out: Completion tokens.
        cache_read_in: Tokens served from the prompt cache (billed at
            ``cache_read`` rate rather than ``input`` rate).
        cache_write_in: Tokens written into the prompt cache this turn
            (billed at ``cache_write`` rate; falls back to
            ``input * 1.25`` when the entry lacks a ``cache_write`` key).
        pricing_table: Dict mapping model name to a rate sub-dict with
            keys ``input``, ``output``, and optionally ``cache_read`` /
            ``cache_write`` (all in USD per 1 M tokens).

    Returns:
        Rounded USD cost (6 decimal places), or ``None`` when inputs are
        insufficient to compute a cost.
    """
    if not model or tokens_in is None or tokens_out is None:
        return None

    entry = pricing_table.get(model)
    if not entry:
        if model not in _warned_models:
            LOGGER.warning(
                "compute_cost_usd: unknown model %r — cost will be None. "
                "Add it to LLM_MAP in scripts/refresh_pricing.py and re-run.",
                model,
            )
            _warned_models.add(model)
        return None

    cache_read = cache_read_in or 0
    cache_write = cache_write_in or 0

    # Tokens billed at the normal input rate are what remains after removing
    # cache hits and cache writes (both have their own billing rates).
    non_cached_in = max(0, tokens_in - cache_read - cache_write)

    cache_write_rate = entry.get("cache_write", entry["input"] * 1.25)

    cost = (
        non_cached_in * entry["input"] / 1_000_000
        + tokens_out * entry["output"] / 1_000_000
        + cache_read * entry.get("cache_read", 0) / 1_000_000
        + cache_write * cache_write_rate / 1_000_000
    )
    return round(cost, 6)


def load_pricing_table(
    pricing_json_path: str | Path = DEFAULT_PRICING_JSON,
    fallback_table: dict | None = None,
) -> dict:
    """Load ``data/pricing.json`` and flatten to the format compute_cost_usd expects.

    The on-disk JSON uses verbose keys (``input_per_1m`` etc.) and groups
    LLM vs. TTS entries. This function flattens only the LLM section into
    the legacy ``{model: {input, output, cache_read, cache_write}}`` shape
    where all values are USD per 1M tokens.

    Args:
        pricing_json_path: Path to ``pricing.json``. Defaults to repo-relative
            ``data/pricing.json``; tests can point this at a fixture.
        fallback_table: Legacy-format table (e.g. ``config.yaml`` ``llm_pricing``)
            returned verbatim when the JSON is missing or malformed. ``None``
            yields an empty dict so cost tracking silently no-ops.

    Returns:
        A legacy-format pricing table dict. Never raises; malformed JSON is
        logged and falls through to ``fallback_table``.
    """
    path = Path(pricing_json_path)
    if not path.exists():
        if fallback_table:
            LOGGER.info(
                "pricing.json not found at %s — using config.yaml fallback (%d models)",
                path,
                len(fallback_table),
            )
            return fallback_table
        LOGGER.warning(
            "pricing.json not found at %s and no fallback — cost tracking disabled. "
            "Run: python scripts/refresh_pricing.py",
            path,
        )
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        LOGGER.warning(
            "pricing.json load failed (%s) — using fallback table", exc,
        )
        return fallback_table or {}

    table: dict[str, dict[str, float]] = {}
    for model, entry in (data.get("llm") or {}).items():
        inp = entry.get("input_per_1m")
        out = entry.get("output_per_1m")
        if inp is None or out is None:
            LOGGER.debug("skipping %s: missing input/output rate", model)
            continue
        row: dict[str, float] = {"input": float(inp), "output": float(out)}
        cr = entry.get("cache_read_per_1m")
        if cr is not None:
            row["cache_read"] = float(cr)
        cw = entry.get("cache_write_per_1m")
        if cw is not None:
            row["cache_write"] = float(cw)
        table[model] = row

    LOGGER.info(
        "loaded pricing.json: %d LLM models (generated %s)",
        len(table),
        data.get("generated_at", "unknown"),
    )
    return table
