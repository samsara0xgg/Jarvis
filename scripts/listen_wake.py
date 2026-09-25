"""Listen on the default microphone; print ``unlock <probability>`` on each wake detection.

    PYTHONPATH=. python scripts/listen_wake.py
    PYTHONPATH=. python scripts/listen_wake.py --engine openwakeword --threshold 0.5

Same engine classes and the same 80 ms / 1280-sample PCM16 frames as the
daemon (ADR-0042); nothing else is printed.  Type a number and Enter while it
runs to change the threshold on the spot.  Ctrl-C stops it.
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time

from jarvis.surface import voice_wake

FRAME_SAMPLES = 1280
REFRACTORY_S = 1.0  # one phrase, one line


def watch_stdin(threshold: list[float]) -> None:
    """Replace ``threshold[0]`` with every number typed on stdin."""
    for line in sys.stdin:
        try:
            threshold[0] = float(line.strip())
        except ValueError:
            print(f"not a number: {line.strip()!r}", file=sys.stderr)
            continue
        print(f"threshold -> {threshold[0]}", file=sys.stderr)


def main() -> None:
    """Run the microphone loop until Ctrl-C."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", default="microwakeword", choices=voice_wake.WAKE_ENGINES)
    ap.add_argument("--threshold", type=float, default=0.95)
    args = ap.parse_args()

    import sounddevice as sd  # noqa: PLC0415

    engine = voice_wake.build_wake_engine(args.engine)
    engine.start()
    frames: queue.Queue[bytes] = queue.Queue()
    stream = sd.InputStream(
        samplerate=16000, channels=1, dtype="int16", blocksize=FRAME_SAMPLES,
        callback=lambda indata, *_: frames.put(bytes(indata)),
    )
    threshold = [args.threshold]
    threading.Thread(target=watch_stdin, args=(threshold,), daemon=True).start()
    print(
        f"listening ({args.engine} >= {threshold[0]}); type a number + Enter to change it; "
        "Ctrl-C to stop",
        file=sys.stderr,
    )
    stream.start()
    muted_until = 0.0
    try:
        while True:
            frame = frames.get()
            prob = engine.predict(frame).get(engine.model_name, 0.0)
            now = time.monotonic()
            if prob >= threshold[0] and now >= muted_until:
                print(f"unlock {prob:.3f}", flush=True)
                engine.reset()
                muted_until = now + REFRACTORY_S
    except KeyboardInterrupt:
        pass
    finally:
        stream.abort()  # not stop(): the drain path deadlocks on CoreAudio's HAL mutex
        stream.close()
        engine.close()


if __name__ == "__main__":
    main()
