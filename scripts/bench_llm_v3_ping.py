#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "anthropic>=0.42",
#   "openai>=1.50",
#   "google-generativeai>=0.8",
#   "groq>=0.11",
#   "tiktoken>=0.8",
#   "tqdm>=4.66",
#   "plotly>=5.18",
#   "pandas>=2.0",
# ]
# ///
"""Ping all non-Anthropic providers in MODEL_CATALOG to verify keys + model IDs work.

Uses the same warmup_one logic as the real benchmark so any issue found here
would also fail --standard. Cost: ~5 calls × ~10 tokens = effectively $0.
"""
from __future__ import annotations
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_llm_v3 as b  # noqa: E402


async def main() -> int:
    b._ensure_google_key_env()
    # Skip Anthropic (already validated in Stage 2 --quick)
    specs = [s for s in b.MODEL_CATALOG if s.provider != "anthropic"]

    print(f"Pinging {len(specs)} non-Anthropic models (timeout 30s each)...\n")

    async def ping(s: b.ModelSpec):
        if not b._provider_has_key(s.provider):
            return (s, None, f"missing {b.API_KEY_ENV[s.provider]}")
        t0 = time.perf_counter()
        try:
            result = await asyncio.wait_for(b.warmup_one(s), timeout=30.0)
        except asyncio.TimeoutError:
            return (s, None, "timeout >30s")
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if result is None:
            return (s, None, "exhausted fallbacks — see log above")
        mid, is_fb = result
        return (s, (mid, is_fb, elapsed_ms), None)

    results = await asyncio.gather(*(ping(s) for s in specs))

    ok = 0
    fail = 0
    for spec, res, err in results:
        label = f"{spec.provider}/{spec.primary_id}"
        if err:
            print(f"  ✗ {label:48s}  {err}")
            fail += 1
        else:
            mid, is_fb, ms = res
            marker = "↪" if is_fb else "✓"
            note = f" (fallback: {mid})" if is_fb else ""
            print(f"  {marker} {label:48s}  {ms:6.0f}ms{note}")
            ok += 1

    print(f"\n{ok}/{len(specs)} providers alive, {fail} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
