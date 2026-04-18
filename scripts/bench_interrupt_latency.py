#!/usr/bin/env python3
"""Interrupt latency benchmark — VAD-confirmed-start → keyword detection.

Half-automated: this script plays a long TTS clip; you say "停" while it
plays. Latency is computed using the SileroVADDirect START timestamp as
``t_speech_start`` and the on_interrupt callback fire time as
``t_detected``.

**What `speech_to_detect_ms` really measures**: the time from
``vad.last_start_perf`` (the moment the VAD state machine transitioned
IDLE→ACTIVE, i.e., ``required_hits`` frames of 32ms each after true
speech onset — typically ~96ms of the onset is baked into the value)
to the ASR keyword callback fire. This captures
``smoothing_window+decoder+keyword-match`` latency; it does NOT
include the first ~96ms of real speech. True speech-onset → detect
would be `speech_to_detect_ms + required_hits * 32ms` in the worst case.

Also: the mic listener is started RIGHT BEFORE playback (not during
the countdown), so VAD sees a clean IDLE→ACTIVE transition when you
speak. Pre-playback ambient noise no longer contaminates the
``last_start_perf`` timestamp.

Usage:
    python scripts/bench_interrupt_latency.py --runs 10
    python scripts/bench_interrupt_latency.py --runs 5 --label after-WP6

Output: appends one JSON line per run to
``scripts/bench_results/interrupt_latency.jsonl`` and prints the median
``speech_to_detect_ms`` over the batch.

Requires ``interrupt.vad_provider: silero_direct`` in config.yaml — the
sherpa_onnx wrapper does not expose START timestamps.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

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
    with (ROOT / "config.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _run_once(label: str, run_index: int) -> dict | None:
    """Play one long TTS clip and time the interrupt detection."""
    from core.interrupt_monitor import InterruptMonitor
    from core.tts import TTSEngine, TTSPipeline, SentenceType
    from core.vad_silero import SileroVADDirect

    cfg = _load_config()
    if cfg.get("interrupt", {}).get("vad_provider") != "silero_direct":
        print("ERROR: interrupt.vad_provider must be 'silero_direct' for B-mode bench")
        return None

    engine = TTSEngine(cfg)
    pipeline = TTSPipeline(engine)

    detected_perf: list[float] = []
    cancelled_perf: list[float] = []
    vad_start_perf_at_detect: list[float | None] = []

    def on_interrupt() -> None:
        t = time.perf_counter()
        detected_perf.append(t)
        # Capture the VAD start timestamp at the exact moment we detect —
        # the same _vad instance is in use across both threads.
        vad = monitor._vad  # noqa: SLF001 — bench harness, intentional
        if isinstance(vad, SileroVADDirect):
            vad_start_perf_at_detect.append(vad.last_start_perf)
        else:
            vad_start_perf_at_detect.append(None)
        pipeline.abort()
        cancelled_perf.append(time.perf_counter())

    monitor = InterruptMonitor(cfg, on_interrupt=on_interrupt)
    if not monitor.enabled:
        print("ERROR: interrupt monitor not enabled in config.yaml")
        return None

    # Instrumentation: tee mic audio + print every ASR partial so we can
    # see in real time whether the mic picked up '停' and whether the
    # streaming ASR produced any recognizable text. Both are gated by
    # instance-attribute monkeypatch so nothing in InterruptMonitor needs
    # to change.
    tee_chunks: list[np.ndarray] = []
    _orig_feed = monitor.feed_audio
    _vad_state = {"was_active": False}

    def _tee_feed(audio: np.ndarray, sample_rate: int = 16000) -> None:
        tee_chunks.append(audio.copy())
        _orig_feed(audio, sample_rate)
        # Log VAD edges so we can correlate "speech onset" with the
        # transcribe/keyword callback that fires later.
        try:
            vad = monitor._vad
            is_active = bool(vad.is_speech_detected()) if vad is not None else False
            if is_active and not _vad_state["was_active"]:
                print(
                    f"  [VAD IDLE→ACTIVE @{time.perf_counter()-play_started:+.2f}s]",
                    flush=True,
                )
            elif not is_active and _vad_state["was_active"]:
                print(
                    f"  [VAD ACTIVE→IDLE @{time.perf_counter()-play_started:+.2f}s]",
                    flush=True,
                )
            _vad_state["was_active"] = is_active
        except Exception:
            pass

    monitor.feed_audio = _tee_feed  # type: ignore[method-assign]

    _orig_check = monitor._check_partial  # bound method  # noqa: SLF001

    def _logged_check(text: str) -> None:
        print(
            f"  [transcript @{time.perf_counter()-play_started:+.2f}s] {text!r}",
            flush=True,
        )
        _orig_check(text)

    monitor._check_partial = _logged_check  # type: ignore[method-assign]  # noqa: SLF001

    # play_started is defined below; bind via closure after we set it
    play_started = 0.0  # placeholder; reassigned before TTS submit

    # B1 fix: DO NOT start the mic listener yet — it would run during the
    # 3-second countdown and potentially cache a spurious VAD START from
    # ambient noise, polluting the speech→detect measurement later.
    monitor.start()
    pipeline.start()

    print(f"\n[run {run_index}] 准备好了说 '停' 来打断 TTS（label={label}）")
    print(f"[run {run_index}] 3 秒后开始播放，听到声音后随时说'停' ↓")
    time.sleep(3)

    # Start the mic listener RIGHT BEFORE playback so VAD sees a clean
    # IDLE→ACTIVE transition when you actually speak. Also reset VAD state
    # defensively in case any other path touched it.
    if isinstance(monitor._vad, SileroVADDirect):
        monitor._vad.reset()
    monitor.start_mic_listener()

    play_started = time.perf_counter()
    pipeline.submit(LONG_TEXT, SentenceType.FIRST)
    pipeline.finish()

    # Wait for either an interrupt or playback completion (60s safety cap).
    deadline = play_started + 60
    while time.perf_counter() < deadline:
        if cancelled_perf:
            break
        # pipeline._done is set when finish() drains; private but stable in this codebase
        if pipeline._done.is_set():  # noqa: SLF001
            break
        time.sleep(0.05)

    monitor.stop_mic_listener()
    monitor.stop()
    pipeline.stop()

    # Save teed mic audio for post-hoc analysis via diag_bench_live.py
    if tee_chunks:
        tee_dir = ROOT / "scripts" / "bench_results" / "bench_mic_tees"
        tee_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_path = tee_dir / f"bench_{label}_run{run_index}_{ts}.wav"
        audio_full = np.concatenate(tee_chunks)
        pcm = (audio_full * 32767).clip(-32768, 32767).astype(np.int16)
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.tobytes())
        print(f"[run {run_index}] mic tee → {wav_path}  ({len(audio_full)/16000:.1f}s)")

    if not detected_perf:
        print(f"[run {run_index}] 没听到打断（可能没说出'停' / 太晚 / 太小声），跳过")
        return None

    t_detect = detected_perf[0]
    t_cancel = cancelled_perf[0]
    t_speech_start = vad_start_perf_at_detect[0]

    if t_speech_start is None:
        print(f"[run {run_index}] VAD 未捕获 START 事件（异常），跳过")
        return None

    speech_to_detect_ms = (t_detect - t_speech_start) * 1000
    detect_to_cancel_ms = (t_cancel - t_detect) * 1000
    total_speech_to_cancel_ms = (t_cancel - t_speech_start) * 1000

    print(
        f"[run {run_index}] speech→detect: {speech_to_detect_ms:.0f}ms  "
        f"detect→cancel: {detect_to_cancel_ms:.0f}ms  "
        f"total: {total_speech_to_cancel_ms:.0f}ms"
    )

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "run_index": run_index,
        "speech_to_detect_ms": round(speech_to_detect_ms, 1),
        "detect_to_cancel_ms": round(detect_to_cancel_ms, 1),
        "total_ms": round(total_speech_to_cancel_ms, 1),
        "min_segment_ms": cfg.get("interrupt", {}).get("min_segment_ms", 150),
        "vad_provider": cfg.get("interrupt", {}).get("vad_provider", "?"),
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
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    samples: list[float] = []
    for i in range(args.runs):
        result = _run_once(args.label, i + 1)
        if result is None:
            continue
        _append_result(result)
        samples.append(result["speech_to_detect_ms"])

    if not samples:
        print("\n没有有效样本")
        return 1

    print()
    print(f"=== batch summary  label={args.label}  runs={len(samples)}/{args.runs} ===")
    print(f"  speech→detect median: {statistics.median(samples):.0f} ms")
    if len(samples) >= 2:
        print(f"  speech→detect min/max: {min(samples):.0f} / {max(samples):.0f} ms")
    print(f"  raw: {[round(s) for s in samples]}")
    print(f"  full log: {RESULTS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
