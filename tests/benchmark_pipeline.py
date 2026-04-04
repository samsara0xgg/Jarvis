"""End-to-end pipeline latency benchmark — tests route cache + TTS cache.

Usage:
    source ~/.secrets
    python tests/benchmark_pipeline.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

# Suppress verbose logs
import logging
logging.basicConfig(level=logging.WARNING)
# But show cache hits
logging.getLogger("core.intent_router").setLevel(logging.INFO)
logging.getLogger("core.tts").setLevel(logging.INFO)


def main() -> None:
    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    # --- Test 1: Route cache ---
    print("\n" + "=" * 60)
    print("  Route Cache Benchmark")
    print("=" * 60)

    from core.intent_router import IntentRouter
    router = IntentRouter(config)

    cases = [
        ("开灯", "smart_home 首次"),
        ("开灯", "smart_home 缓存"),
        ("关灯", "smart_home 首次"),
        ("关灯", "smart_home 缓存"),
        ("几点了", "time 首次"),
        ("几点了", "time 缓存"),
        ("今天天气怎么样", "info_query 首次"),
        ("今天天气怎么样", "info_query 缓存"),
        ("帮我写首诗", "complex 首次"),
        ("帮我写首诗", "complex 缓存"),
        ("开灯", "smart_home 再次缓存"),
    ]

    print(f"\n  {'描述':20s}  {'耗时':>7s}  {'意图':12s}  {'provider':10s}  缓存")
    print(f"  {'-'*70}")

    for text, label in cases:
        t0 = time.perf_counter()
        result = router.route(text)
        ms = (time.perf_counter() - t0) * 1000
        cached = "HIT" if ms < 1 else ""
        print(f"  {label:20s}  {ms:6.1f}ms  {result.intent:12s}  {result.provider:10s}  {cached}")

    print(f"\n  Cache size: {len(router._route_cache)} entries")

    # --- Test 2: TTS cache ---
    print("\n" + "=" * 60)
    print("  TTS Cache Benchmark")
    print("=" * 60)

    from core.tts import TTSEngine
    tts = TTSEngine(config)

    tts_cases = [
        ("好的，灯开了。", "calm", "短回复 首次"),
        ("好的，灯开了。", "calm", "短回复 缓存"),
        ("好的，灯关了。", "calm", "不同文本 首次"),
        ("好的，灯关了。", "calm", "不同文本 缓存"),
        ("好的，灯开了。", "calm", "再次缓存"),
    ]

    if tts.minimax_key:
        print(f"\n  {'描述':16s}  {'耗时':>7s}  缓存  文件")
        print(f"  {'-'*60}")

        for text, emotion, label in tts_cases:
            t0 = time.perf_counter()
            path, deletable = tts._synth_minimax(text, emotion)
            ms = (time.perf_counter() - t0) * 1000
            cached = "HIT" if not deletable and ms < 50 else ("MISS" if deletable else "SAVED")
            print(f"  {label:16s}  {ms:6.0f}ms  {cached:5s}  {os.path.basename(path)}")
            if deletable:
                os.unlink(path)
    else:
        print("\n  SKIPPED — no MINIMAX_API_KEY")

    # --- Test 3: Embedder cache ---
    print("\n" + "=" * 60)
    print("  Embedder Cache Benchmark")
    print("=" * 60)

    from memory.embedder import Embedder
    embedder = Embedder()

    embed_cases = [
        ("开灯", "首次 encode"),
        ("开灯", "同文本 缓存"),
        ("关灯", "不同文本"),
        ("关灯", "同文本 缓存"),
        ("开灯", "切回首次文本"),
    ]

    print(f"\n  {'描述':16s}  {'耗时':>7s}")
    print(f"  {'-'*30}")

    for text, label in embed_cases:
        t0 = time.perf_counter()
        embedder.encode(text)
        ms = (time.perf_counter() - t0) * 1000
        print(f"  {label:16s}  {ms:6.1f}ms")

    print("\n" + "=" * 60)
    print("  Done!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
