"""Replay labelled wake recordings through the daemon's wake engines (ADR-0042).

    python scripts/replay_wake_engine.py --labels ~/.jarvis-kws-bench/live_mww/labels.json
    python scripts/replay_wake_engine.py --labels ... --engine openwakeword --threshold 0.5

Each wav is cut into the daemon's own 80 ms / 1280-sample PCM16 frames and fed
to ``engine.predict`` one frame at a time; a file wakes when any frame's
probability reaches the threshold.  Wav keys in the labels file resolve
relative to that file's directory; ``should_wake`` is the human label.
"""
from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

from jarvis.surface import voice_wake

FRAME_BYTES = 1280 * 2
DEFAULT_THRESHOLD = {"microwakeword": 0.95, "openwakeword": 0.5}


def peak_probability(engine: voice_wake.AnyWakeEngine, wav: Path) -> float:
    """Highest per-frame probability the engine reports over one file."""
    with wave.open(str(wav)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
            msg = f"{wav}: need 16 kHz mono PCM16"
            raise SystemExit(msg)
        pcm = w.readframes(w.getnframes())
    engine.reset()
    peak = 0.0
    for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES):
        prob = engine.predict(pcm[i : i + FRAME_BYTES]).get(engine.model_name, 0.0)
        peak = max(peak, prob)
    return peak


def main() -> None:
    """Score every labelled wav with the chosen engine(s) and tally against the labels."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--labels", required=True, type=Path)
    ap.add_argument("--engine", default="both", choices=(*voice_wake.WAKE_ENGINES, "both"))
    ap.add_argument("--threshold", type=float, default=None)
    args = ap.parse_args()
    labels_path = args.labels.expanduser()
    labels = json.loads(labels_path.read_text())
    kinds = list(voice_wake.WAKE_ENGINES) if args.engine == "both" else [args.engine]
    engines: dict[str, voice_wake.AnyWakeEngine] = {}
    for kind in kinds:
        engines[kind] = voice_wake.build_wake_engine(kind)
        engines[kind].start()
    thresholds = {k: (args.threshold or DEFAULT_THRESHOLD[k]) for k in kinds}
    print("file          label  " + "  ".join(f"{k}@{thresholds[k]}" for k in kinds))
    stats = {k: {"pos": 0, "neg": 0, "hit": 0, "fa": 0} for k in kinds}
    for name, meta in sorted(labels.items()):
        should = meta.get("should_wake")
        if should is None:
            continue
        cells = []
        for kind, engine in engines.items():
            prob = peak_probability(engine, labels_path.parent / name)
            woke = prob >= thresholds[kind]
            tally = stats[kind]
            tally["pos" if should else "neg"] += 1
            tally["hit" if should else "fa"] += int(woke)
            mark = "" if woke == should else " <-- wrong"
            cells.append(f"{prob:.3f} {'wake' if woke else 'quiet'}{mark}")
        kind_label = "pos" if should else "neg"
        print(f"{name:13s} {kind_label:5s}  " + "  ".join(f"{c:24s}" for c in cells))
    for kind, tally in stats.items():
        print(
            f"{kind}@{thresholds[kind]}: wake {tally['hit']}/{tally['pos']} positives, "
            f"false accept {tally['fa']}/{tally['neg']} negatives",
        )


if __name__ == "__main__":
    main()
