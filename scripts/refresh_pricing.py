"""Refresh LLM + TTS pricing from LiteLLM's community-maintained JSON.

Fetches ``model_prices_and_context_window.json`` from the upstream LiteLLM
repo, extracts pricing for every model Jarvis actually uses (chat LLMs +
TTS engines), and writes a normalised ``data/pricing.json`` keyed by the
internal Jarvis model ID.

Also prints a diff vs. the ``llm_pricing`` block in ``config.yaml`` so
Allen can review before replacing it by hand (or leave the JSON as the
new source of truth).

Run::

    python scripts/refresh_pricing.py            # fetch + write + diff
    python scripts/refresh_pricing.py --offline  # reuse cached JSON in /tmp
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
CACHE_PATH = Path("/tmp/litellm_prices.json")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
OUT_PATH = REPO_ROOT / "data" / "pricing.json"

LOGGER = logging.getLogger("refresh_pricing")


# Map internal Jarvis model IDs -> LiteLLM keys.
# Left side is what appears in ``trace.llm_model`` / ``config.yaml``.
# Right side must exist in the LiteLLM JSON. A ``None`` value marks an
# alias of the previous real entry (kept so pricing.json has both forms).
LLM_MAP: dict[str, str] = {
    # xAI — trace and config use the bare id without the ``xai/`` prefix.
    "grok-4.20-0309-non-reasoning":    "xai/grok-4.20-beta-0309-non-reasoning",
    "grok-4.20":                       "xai/grok-4.20-beta-0309-non-reasoning",
    "grok-reasoning":                  "xai/grok-4.20-beta-0309-reasoning",
    "grok-4.1-fast-non-reasoning":     "xai/grok-4-1-fast-non-reasoning",
    "grok-4-1-fast":                   "xai/grok-4-1-fast-non-reasoning",
    "grok-4.1-fast-reasoning":         "xai/grok-4-1-fast-reasoning",
    # Anthropic
    "claude-opus-4-7":   "claude-opus-4-7",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    # Groq (router primary)
    "llama-3.3-70b-versatile": "groq/llama-3.3-70b-versatile",
    "llama-3.3-70b":           "groq/llama-3.3-70b-versatile",
    # Cerebras (router fallback)
    "llama3.1-8b": "cerebras/llama3.1-8b",
    # OpenAI
    "gpt-4o-mini":  "gpt-4o-mini",
    "gpt-5.4":      "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    # Google (observer fallback)
    "gemini-2.5-flash": "gemini-2.5-flash",
}

# TTS pricing: OpenAI bills per token, MiniMax per character.
# ``speech-2.8-turbo`` is not in LiteLLM yet; fall back to ``speech-2.6-turbo``
# which shares the same published rate ($0.06/1M chars).
TTS_MAP: dict[str, str] = {
    "gpt-4o-mini-tts":   "gpt-4o-mini-tts",
    "tts-1":             "tts-1",
    "tts-1-hd":          "tts-1-hd",
    "speech-02-turbo":   "minimax/speech-02-turbo",
    "speech-02-hd":      "minimax/speech-02-hd",
    "speech-2.6-turbo":  "minimax/speech-2.6-turbo",
    "speech-2.6-hd":     "minimax/speech-2.6-hd",
    "speech-2.8-turbo":  "minimax/speech-2.6-turbo",  # not in LiteLLM yet, same rate
}


def fetch_litellm(offline: bool) -> dict[str, Any]:
    """Download (or reuse) the upstream LiteLLM pricing JSON."""
    if offline and CACHE_PATH.exists():
        LOGGER.info("offline mode: reading %s", CACHE_PATH)
        return json.loads(CACHE_PATH.read_text())
    LOGGER.info("fetching %s", LITELLM_URL)
    with urllib.request.urlopen(LITELLM_URL, timeout=30) as resp:
        raw = resp.read()
    CACHE_PATH.write_bytes(raw)
    return json.loads(raw)


def _per_million(v: float | None) -> float | None:
    """LiteLLM stores prices per single token/char; normalise to per 1M."""
    return None if v is None else round(v * 1_000_000, 6)


def extract_llm(jarvis_id: str, litellm_id: str, src: dict[str, Any]) -> dict[str, Any] | None:
    entry = src.get(litellm_id)
    if not entry:
        LOGGER.warning("LLM %r -> %r: missing in LiteLLM", jarvis_id, litellm_id)
        return None
    return {
        "litellm_id": litellm_id,
        "provider": entry.get("litellm_provider"),
        "input_per_1m": _per_million(entry.get("input_cost_per_token")),
        "output_per_1m": _per_million(entry.get("output_cost_per_token")),
        "cache_read_per_1m": _per_million(entry.get("cache_read_input_token_cost")),
        "cache_write_per_1m": _per_million(entry.get("cache_creation_input_token_cost")),
        "supports_prompt_caching": entry.get("supports_prompt_caching", False),
        "source": entry.get("source"),
    }


def extract_tts(jarvis_id: str, litellm_id: str, src: dict[str, Any]) -> dict[str, Any] | None:
    entry = src.get(litellm_id)
    if not entry:
        LOGGER.warning("TTS %r -> %r: missing in LiteLLM", jarvis_id, litellm_id)
        return None
    char_in = entry.get("input_cost_per_character")
    tok_in = entry.get("input_cost_per_token")
    if char_in is not None:
        unit = "character"
    elif tok_in is not None:
        unit = "token"
    else:
        unit = "unknown"
    return {
        "litellm_id": litellm_id,
        "provider": entry.get("litellm_provider"),
        "unit": unit,
        "input_per_1m_chars": _per_million(char_in),
        "input_per_1m_tokens": _per_million(tok_in),
        "output_per_1m_audio_tokens": _per_million(entry.get("output_cost_per_token")),
        "output_per_second": entry.get("output_cost_per_second"),
    }


def build_pricing(src: dict[str, Any]) -> dict[str, Any]:
    llm: dict[str, dict[str, Any]] = {}
    for jid, lid in LLM_MAP.items():
        row = extract_llm(jid, lid, src)
        if row is not None:
            llm[jid] = row
    tts: dict[str, dict[str, Any]] = {}
    for jid, lid in TTS_MAP.items():
        row = extract_tts(jid, lid, src)
        if row is not None:
            tts[jid] = row
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "litellm",
        "source_url": LITELLM_URL,
        "notes": {
            "unit": "All *_per_1m values are USD per 1,000,000 tokens or characters.",
            "speech-2.8-turbo": (
                "Not yet in LiteLLM; mapped to speech-2.6-turbo which shares the "
                "same published rate ($0.06/1M chars). Re-verify when upstream lands."
            ),
        },
        "llm": llm,
        "tts": tts,
    }


def diff_vs_config(new_llm: dict[str, dict[str, Any]]) -> None:
    """Print per-field diff between new LiteLLM prices and config.yaml."""
    if not CONFIG_PATH.exists():
        return
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    old = cfg.get("llm_pricing") or {}
    if not old:
        return

    print("\nDiff vs config.yaml llm_pricing (per 1M USD):")
    print(f"  {'model':38s}  {'field':12s}  {'config':>10s}  {'litellm':>10s}  delta")
    print(f"  {'-'*38}  {'-'*12}  {'-'*10}  {'-'*10}  -----")

    field_map = [
        ("input",       "input_per_1m"),
        ("output",      "output_per_1m"),
        ("cache_read",  "cache_read_per_1m"),
        ("cache_write", "cache_write_per_1m"),
    ]

    seen = set()
    for model, new in sorted(new_llm.items()):
        old_entry = old.get(model)
        seen.add(model)
        if not old_entry:
            print(f"  {model:38s}  {'(new)':12s}  {'-':>10s}  ok")
            continue
        for cfg_key, new_key in field_map:
            o = old_entry.get(cfg_key)
            n = new[new_key]
            if o is None and n is None:
                continue
            if o == n:
                continue
            o_str = "-" if o is None else f"{o:.3f}"
            n_str = "-" if n is None else f"{n:.3f}"
            delta = "?" if (o is None or n is None) else f"{n-o:+.3f}"
            print(f"  {model:38s}  {cfg_key:12s}  {o_str:>10s}  {n_str:>10s}  {delta}")
    for model in sorted(old):
        if model not in seen:
            print(f"  {model:38s}  (in config.yaml only — not in new map)")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="reuse cached /tmp JSON")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help=f"output path (default {OUT_PATH})")
    args = parser.parse_args()

    src = fetch_litellm(args.offline)
    LOGGER.info("loaded %d LiteLLM entries", len(src))

    pricing = build_pricing(src)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pricing, indent=2, ensure_ascii=False) + "\n")
    LOGGER.info(
        "wrote %s — %d LLM, %d TTS",
        args.out,
        len(pricing["llm"]),
        len(pricing["tts"]),
    )

    diff_vs_config(pricing["llm"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
