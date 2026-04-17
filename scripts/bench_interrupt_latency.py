#!/usr/bin/env python3
"""Interrupt latency benchmark.

Measures the time from when the user says an interrupt keyword until
TTS playback actually stops. Half-automated: this script plays a long
TTS clip and records the timestamps; the human says "停" at the right
moment, and the script logs the delta.

Usage:
    python scripts/bench_interrupt_latency.py --runs 10
    python scripts/bench_interrupt_latency.py --runs 1 --label "after-WP6"

Output: appends one JSON line per run to scripts/bench_results/interrupt_latency.jsonl
and prints the median over all runs in this batch.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOGGER = logging.getLogger("bench_interrupt_latency")

RESULTS_DIR = ROOT / "scripts" / "bench_results"
RESULTS_FILE = RESULTS_DIR / "interrupt_latency.jsonl"

LONG_TEXT = (
    "下面是一段比较长的测试文本，我会一直说下去，直到你打断我为止。"
    "今天的天气其实挺不错的，阳光明媚，温度适宜，是个出门散步的好日子。"
    "我们可以聊聊最近发生的有趣的事情，或者讨论一下未来的计划。"
    "你想听什么呢，工作的事情，还是生活上的琐事，都可以随便说。"
    "Jarvis 会一直陪着你，无论是开心还是难过，我都在这里。"
)


def _load_config() -> dict:
    import yaml
    cfg_path = ROOT / "config.yaml"
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _run_once(label: str, run_index: int) -> dict | None:
    """Play one long TTS clip and time interrupt detection."""
    from core.interrupt_monitor import InterruptMonitor
    from core.tts import TTSEngine, TTSPipeline, SentenceType

    cfg = _load_config()
    engine = TTSEngine(cfg)
    pipeline = TTSPipeline(engine)

    detected_at: list[float] = []
    play_started_at: list[float] = []
    cancelled_at: list[float] = []

    def on_interrupt() -> None:
        detected_at.append(time.perf_counter())
        pipeline.abort()
        cancelled_at.append(time.perf_counter())

    monitor = InterruptMonitor(cfg, on_interrupt=on_interrupt)
    if not monitor.enabled:
        LOGGER.error(
            "Interrupt monitor not enabled in config.yaml — cannot benchmark.",
        )
        return None

    monitor.start()
    monitor.start_mic_listener()
    pipeline.start()

    print(f"[run {run_index}] 准备好了说 '停' 来打断 TTS（label={label}）...")
    print(f"[run {run_index}] 3 秒后开始播放 ↓")
    time.sleep(3)

    play_started_at.append(time.perf_counter())
    pipeline.submit(LONG_TEXT, SentenceType.FIRST)
    pipeline.finish()

    # Wait for either pipeline to finish or user to interrupt.
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline:
        if cancelled_at:
            break
        if pipeline._done.is_set():
            break
        time.sleep(0.05)

    monitor.stop_mic_listener()
    monitor.stop()
    pipeline.stop()

    if not detected_at:
        print(f"[run {run_index}] 未检测到打断（也许没说 '停' 或时间不够），跳过这次")
        return None

    # Best signal we have on the bench harness side: detected→cancelled
    # (does not include pre-detect mic→ASR latency, which is the main
    # thing we want — see "limitations" in module docstring).
    detect_to_cancel_ms = (cancelled_at[0] - detected_at[0]) * 1000
    play_to_detect_ms = (detected_at[0] - play_started_at[0]) * 1000

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "run_index": run_index,
        "play_to_detect_ms": round(play_to_detect_ms, 1),
        "detect_to_cancel_ms": round(detect_to_cancel_ms, 1),
        "chunk_samples": cfg.get("interrupt", {}).get(
            "streaming_asr_chunk_samples", 8000),
    }


def _append_result(result: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1, help="how many runs in this batch")
    parser.add_argument(
        "--label",
        default="adhoc",
        help="label for this batch (e.g. 'baseline', 'after-WP1', 'after-WP6')",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    detect_to_cancel_samples: list[float] = []
    for i in range(args.runs):
        result = _run_once(args.label, i + 1)
        if result is None:
            continue
        _append_result(result)
        detect_to_cancel_samples.append(result["detect_to_cancel_ms"])
        print(
            f"[run {i + 1}/{args.runs}] detect→cancel={result['detect_to_cancel_ms']}ms "
            f"play→detect={result['play_to_detect_ms']}ms"
        )

    if not detect_to_cancel_samples:
        print("没有有效样本")
        return 1

    median_ms = statistics.median(detect_to_cancel_samples)
    print()
    print(f"label={args.label}  runs={len(detect_to_cancel_samples)}")
    print(f"  detect→cancel median: {median_ms:.1f} ms")
    print(f"  raw: {detect_to_cancel_samples}")
    print(f"  full log: {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
