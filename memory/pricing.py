"""LLM cost calculator for trace instrumentation.

Converts token counts from a single LLM turn into a USD cost using a
pricing table loaded from config.yaml (``llm_pricing`` key).
"""

import logging

LOGGER = logging.getLogger(__name__)

# Module-level set so each unknown model is warned exactly once per process.
_warned_models: set[str] = set()


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
                "Add it to llm_pricing in config.yaml.",
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
