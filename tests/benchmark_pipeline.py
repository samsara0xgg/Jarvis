"""End-to-end pipeline latency benchmark — tests route cache + TTS cache.

Usage:
    source ~/.secrets
    python tests/benchmark_pipeline.py
"""

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from tests.helpers import REPO_ROOT

# Suppress verbose logs
logging.basicConfig(level=logging.WARNING)
# But show cache hits
logging.getLogger("core.intent_router").setLevel(logging.INFO)
logging.getLogger("core.tts").setLevel(logging.INFO)

log = logging.getLogger("benchmark_pipeline")
log.setLevel(logging.INFO)


def main() -> None:
    with open(REPO_ROOT / "config.yaml") as f:
        config = yaml.safe_load(f)

    if not config:
        log.error("config.yaml is empty or invalid")
        return

    # --- Test 1: Route cache ---
    log.info("\n" + "=" * 60)
    log.info("  Route Cache Benchmark")
    log.info("=" * 60)

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

    log.info(f"\n  {'描述':20s}  {'耗时':>7s}  {'意图':12s}  {'provider':10s}  缓存")
    log.info(f"  {'-'*70}")

    for text, label in cases:
        t0 = time.perf_counter()
        result = router.route(text)
        ms = (time.perf_counter() - t0) * 1000
        cached = "HIT" if ms < 1 else ""
        log.info(f"  {label:20s}  {ms:6.1f}ms  {result.intent:12s}  {result.provider:10s}  {cached}")

    log.info(f"\n  Cache size: {router.cache_size} entries")

    # --- Test 2: TTS cache ---
    log.info("\n" + "=" * 60)
    log.info("  TTS Cache Benchmark")
    log.info("=" * 60)

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
        log.info(f"\n  {'描述':16s}  {'耗时':>7s}  缓存  文件")
        log.info(f"  {'-'*60}")

        for text, emotion, label in tts_cases:
            t0 = time.perf_counter()
            path, deletable = tts._synth_minimax(text, emotion)
            ms = (time.perf_counter() - t0) * 1000
            cached = "HIT" if not deletable and ms < 50 else ("MISS" if deletable else "SAVED")
            log.info(f"  {label:16s}  {ms:6.0f}ms  {cached:5s}  {os.path.basename(path)}")
            if deletable:
                os.unlink(path)
    else:
        log.info("\n  SKIPPED — no MINIMAX_API_KEY")

    # --- Test 3: Embedder cache ---
    log.info("\n" + "=" * 60)
    log.info("  Embedder Cache Benchmark")
    log.info("=" * 60)

    from memory.core.embedder import Embedder
    embedder = Embedder()

    embed_cases = [
        ("开灯", "首次 encode"),
        ("开灯", "同文本 缓存"),
        ("关灯", "不同文本"),
        ("关灯", "同文本 缓存"),
        ("开灯", "切回首次文本"),
    ]

    log.info(f"\n  {'描述':16s}  {'耗时':>7s}")
    log.info(f"  {'-'*30}")

    for text, label in embed_cases:
        t0 = time.perf_counter()
        embedder.encode(text)
        ms = (time.perf_counter() - t0) * 1000
        log.info(f"  {label:16s}  {ms:6.1f}ms")

    log.info("\n" + "=" * 60)
    log.info("  Done!")
    log.info("=" * 60 + "\n")


if __name__ == "__main__":
    main()
