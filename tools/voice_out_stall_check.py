"""GIL-stall proof for the native voice output (ADR 0129).

    .venv/bin/python tools/voice_out_stall_check.py --backend python|native [--seconds 20]
        [--stall c|py] [--device "BlackHole 16ch"]

Plays speech-like audio through one backend to the named device while a thread
holds the GIL for 150 ms once per second, then counts coreaudiod's
"skipping cycle due to overload" / "Overload possibly due to HAL client" lines
(from ``/usr/bin/log show``) per pid, and the player's own ``underflow_count``
and ``starvation_gaps``.  ``--stall c`` holds the GIL inside one C call (what a
native extension does: the interpreter cannot preempt it); ``--stall py`` is a
pure-Python loop, which CPython preempts every 5 ms.  Audio goes only to
``--device``; the default is BlackHole 16ch, never the speakers.
"""

# ruff: noqa: T201, S603, S101, DTZ005, PLR2004, PLR0915, D103, E501

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.surface.voice_ledger import GenerationLease, StalePlaybackGeneration
from jarvis.surface.voice_native_out import NativeAudioStreamPlayer
from jarvis.surface.voice_tts import AudioStreamPlayer

RATE = 48_000
STALL_S = 0.150
LOG_PREDICATE = (
    'eventMessage CONTAINS "skipping cycle due to overload" '
    'OR eventMessage CONTAINS "Overload possibly due to HAL client"'
)


def speech_like(seconds: float) -> np.ndarray:
    """Voiced bursts at a syllable rate: harmonics of 140 Hz under a 4 Hz envelope."""
    t = np.arange(int(RATE * seconds)) / RATE
    voice = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 8))
    envelope = np.clip(np.sin(2 * np.pi * 4 * t), 0, None) * (np.sin(2 * np.pi * 0.7 * t) > -0.6)
    return (0.25 * voice * envelope).astype(np.float32)


def calibrate_c_hold() -> int:
    """``sum(range(n))`` runs in C without releasing the GIL; find n for ``STALL_S``."""
    n = 1_000_000
    while True:
        start = time.perf_counter()
        sum(range(n))
        elapsed = time.perf_counter() - start
        if elapsed > 0.02:
            return int(n * STALL_S / elapsed)
        n *= 4


def stall_loop(kind: str, stop: threading.Event, c_n: int, holds: list[float]) -> None:
    while not stop.wait(1.0 - STALL_S):
        start = time.perf_counter()
        if kind == "c":
            sum(range(c_n))
        else:
            while time.perf_counter() - start < STALL_S:
                pass
        holds.append(time.perf_counter() - start)


def overload_lines(since: datetime) -> list[str]:
    out = subprocess.run(
        ["/usr/bin/log", "show", "--start", since.strftime("%Y-%m-%d %H:%M:%S"),
         "--predicate", LOG_PREDICATE],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return [
        line for line in out.splitlines()
        if re.match(r"^\d{4}-", line) and "com.apple.log" not in line
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["python", "native"], required=True)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--stall", choices=["c", "py"], default="c")
    ap.add_argument("--device", default="BlackHole 16ch")
    args = ap.parse_args()

    # The daemon's own settings (inherent_loop._build_tts_pipeline).
    common = {
        "sample_rate_hz": RATE, "ring_seconds": 2.0, "generation_safe": True,
        "device": args.device, "lazy_open": True,
    }
    player: AudioStreamPlayer
    if args.backend == "python":
        player = AudioStreamPlayer(blocksize=2048, latency=0.12, **common)  # type: ignore[arg-type]
    else:
        player = NativeAudioStreamPlayer(**common)  # type: ignore[arg-type]
    started = player.start()
    if not started.started:
        print(f"start failed: {started.status} {started.reason}")
        return 2
    helper_pid = getattr(getattr(player, "_proc", None), "pid", None)
    pids = {os.getpid()} | ({helper_pid} if helper_pid else set())

    samples = speech_like(args.seconds)
    lease = player.activate_generation(
        session_id="S", response_id="R", response_group_id="G", turn_id="T",
    )
    assert isinstance(lease, GenerationLease)
    gen = lease.playback_generation_id
    player.begin_generation_segment(
        expected_playback_generation_id=gen, sequence=0, text="speech", segment_hash="h",
    )
    stop = threading.Event()
    holds: list[float] = []
    c_n = calibrate_c_hold() if args.stall == "c" else 0
    started_at = datetime.now().replace(microsecond=0)
    t0 = time.monotonic()
    stall = threading.Thread(target=stall_loop, args=(args.stall, stop, c_n, holds), daemon=True)
    stall.start()

    offset = 0
    while offset < len(samples):
        result = player.write_generation(
            samples[offset:].tobytes(), expected_playback_generation_id=gen, segment_sequence=0,
        )
        if result is None:
            player.poll_presentation()
            time.sleep(0.002)
            continue
        assert not isinstance(result, StalePlaybackGeneration)
        offset += result.sample_count
    player.finish_generation_segment(expected_playback_generation_id=gen, sequence=0)
    deadline = time.monotonic() + args.seconds + 10
    while time.monotonic() < deadline:
        snap = player.poll_generation(gen)
        if not isinstance(snap, StalePlaybackGeneration) and snap.fully_presented:
            break
        time.sleep(0.01)
    elapsed = time.monotonic() - t0
    stop.set()
    stall.join()
    underflows, gaps, callbacks = player.underflow_count, player.starvation_gaps, player.callback_calls
    player.stop()
    time.sleep(2.0)  # coreaudiod writes its lines a moment after the cycle

    lines = overload_lines(started_at)
    # The client's own line ("skipping cycle") carries its pid; coreaudiod's
    # "Overload possibly due to HAL client" line carries coreaudiod's, one per cycle.
    skipped = [line for line in lines if "skipping cycle" in line]
    by_pid = Counter(line.split()[5] for line in skipped)
    ours = sum(n for pid, n in by_pid.items() if int(pid) in pids)
    print(f"backend={args.backend} stall={args.stall} device={args.device!r} "
          f"played={elapsed:.1f}s callbacks={callbacks}")
    print(f"gil_holds={len(holds)} mean_hold_ms={1000 * sum(holds) / max(1, len(holds)):.0f}")
    print(f"our_pids={sorted(pids)} (script, helper)")
    print(f"skipping_cycle_lines={len(skipped)} by_pid={dict(by_pid)} OURS={ours} "
          f"coreaudiod_overload_lines={len(lines) - len(skipped)}")
    print(f"underflow_count={underflows} starvation_gaps={gaps}")
    for line in lines[:6]:
        print("  ", line[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
