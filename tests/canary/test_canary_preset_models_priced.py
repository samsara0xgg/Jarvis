"""Preset-pricing canary: every shipped preset model has a priceable row.

``compute_cost_usd`` returns ``None`` for a model missing from the pricing
table (``jarvis/shared/pricing.py``), so ``cost.recorded`` still commits but
carries ``cost_usd: null`` — cost accounting goes blind without anything
failing. The 2026-09-04 DeepSeek preset switch did exactly that to 100% of
real traffic, because ``data/pricing.json`` was generated against an older
model set.

This canary reads the shipped presets out of ``config/jarvis.yaml`` and
asserts each preset's model resolves to a priceable entry. It compares
against ``load_pricing_table`` output rather than the raw JSON ``llm`` keys
on purpose: a row whose ``input_per_1m`` or ``output_per_1m`` is null is
silently dropped by the loader (``jarvis/shared/pricing.py:134-139``) and
would still price as ``None``.

The canary only reads ``config/jarvis.yaml``; it never writes it.
"""

from __future__ import annotations

import yaml

from jarvis.shared.pricing import load_pricing_table
from tests.canary._helpers import repo_root


def _preset_models() -> dict[str, str]:
    """Return ``{preset name: model id}`` for every ``llm.presets.*`` entry."""
    config = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    presets = (config.get("llm") or {}).get("presets") or {}
    return {name: body["model"] for name, body in presets.items() if body.get("model")}


def test_canary_preset_models_priced() -> None:
    """Fail if any shipped preset's model has no priceable pricing.json row."""
    models = _preset_models()
    assert models, "config/jarvis.yaml declares no llm.presets.*.model to price"

    priced = set(load_pricing_table(repo_root() / "data" / "pricing.json"))
    missing = sorted(
        f"{preset} -> {model}" for preset, model in models.items() if model not in priced
    )

    assert not missing, (
        "preset-models-priced canary — these config/jarvis.yaml presets have no "
        "priceable row in data/pricing.json, so every cost.recorded for them "
        "carries cost_usd=null:\n  "
        + "\n  ".join(missing)
        + "\nRemedy: add the model to LLM_MAP in scripts/refresh_pricing.py and "
        "re-run the script to regenerate data/pricing.json."
    )
