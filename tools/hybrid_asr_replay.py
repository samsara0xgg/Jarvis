"""Replay recorded voice turns through the real ``HybridFinalRecognizer`` (ADR 0132).

    .venv/bin/python tools/hybrid_asr_replay.py [--ids FILE] [--count 80] [--out FILE]

Each ``~/.jarvis/memory/audio/T*.m4a`` is decoded (afconvert, 16 kHz mono PCM16) and run through
Silero in 32 ms frames. From the first speech frame the clock then runs in real time, the way the
duplex session sees the turn: ``prepare`` at the 6th silent frame of every pause, ``discard`` when
speech resumes, ``recognize_prepared`` at the 24th silent frame (the acoustic endpoint). The extra
wait is how much later the hybrid answers than SenseVoice alone would, from the same endpoint.
Needs the live models under ``~/.jarvis/models`` and mlx-whisper; it shares the GPU with the
daemon, as the live path does. ``--ids`` takes a JSON list of rows with a ``turn_id`` (the
asr_compare ``results.json``) to replay those turns instead of the newest ``--count``.
"""

# ruff: noqa: T201, S603, S607 - a report printed to the terminal; afconvert is a fixed system tool

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # the venv's editable install may point at another checkout
    sys.path.insert(0, str(REPO_ROOT))

from jarvis.runtime.dictation import whisper_ears  # noqa: E402
from jarvis.surface import voice_asr, voice_audio, voice_session  # noqa: E402

AUDIO = Path.home() / ".jarvis/memory/audio"
MODELS = Path.home() / ".jarvis/models"
FRAME_BYTES = voice_audio.SILERO_CHUNK_SAMPLES * 2
FRAME_S = voice_audio.SILERO_CHUNK_SAMPLES / 16_000
MIN_CLIP_S = 0.4  # asr_compare dropped shorter recordings


def decode(path: Path, tmp: Path) -> bytes:
    """One recording as 16 kHz mono PCM16."""
    wav = tmp / f"{path.stem}.wav"
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(wav)],
        check=True,
    )
    with wave.open(str(wav)) as reader:
        pcm: bytes = reader.readframes(reader.getnframes())
    return pcm


def select(ids: Path | None, count: int) -> list[tuple[str, bytes]]:
    """The turns to replay: those named in ``ids``, else the newest ``count``."""
    if ids is None:
        paths = sorted(AUDIO.glob("T*.m4a"), key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        rows = json.loads(ids.read_text(encoding="utf-8"))
        paths = [AUDIO / f"{row['turn_id']}.m4a" for row in rows if row["turn_id"].startswith("T")]
    turns: list[tuple[str, bytes]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            pcm = decode(path, Path(tmp))
            if len(pcm) / 32_000 >= MIN_CLIP_S:
                turns.append((path.stem, pcm))
            if len(turns) == count:
                break
    return turns


def speech_flags(vad: voice_audio.SileroVad, pcm: bytes) -> tuple[list[bool], int | None]:
    """Per-frame speech flags, and the frame where the VAD went ACTIVE (speech onset)."""
    vad.prepare_utterance()
    flags: list[bool] = []
    first: int | None = None
    for i in range(len(pcm) // FRAME_BYTES):
        event = vad.feed(pcm[i * FRAME_BYTES : (i + 1) * FRAME_BYTES])
        flags.append(event is voice_audio.VadEvent.SPEECH_ACTIVE)
        if first is None and vad.is_speech_detected():
            first = i
    return flags, first


def plan(flags: list[bool], first: int, endpoint: int) -> list[tuple[int, str, int]]:
    """``(frame, action, speech frames so far)``: the session's hints and the endpoint, in order."""
    events: list[tuple[int, str, int]] = []
    last, silent, prepared = first, 0, False
    for i in range(first, len(flags)):
        if flags[i]:
            last, silent = i, 0
            if prepared:
                events.append((i, "discard", 0))
                prepared = False
            continue
        silent += 1
        if silent == voice_session._PREPARE_SILENT_FRAMES:  # noqa: SLF001 - the session's constant
            events.append((i, "prepare", last - first + 1))
            prepared = True
        if silent == endpoint:
            break
    else:
        i = len(flags) - 1  # the clip ends before the endpoint: commit at its end
    events.append((i, "commit", last - first + 1))
    return events


def replay(
    ears: voice_asr.HybridFinalRecognizer,
    turn: str,
    pcm: bytes,
    first: int,
    events: list[tuple[int, str, int]],
) -> dict[str, Any]:
    """Make each call of ``events`` at its time from speech onset; time the commit's wait."""
    uid = f"U{turn}"
    base = time.perf_counter()
    prepares = discards = 0
    for frame, action, speech_frames in events:
        time.sleep(max(0.0, base + (frame + 1 - first) * FRAME_S - time.perf_counter()))
        audio = pcm[: (frame + 1) * FRAME_BYTES]
        if action == "prepare":
            ears.prepare(uid, audio, speech_frames * FRAME_S)
            prepares += 1
        elif action == "discard":
            ears.discard(uid)
            discards += 1
    committed_at = time.perf_counter()
    result = ears.recognize_prepared(uid, audio, speech_frames * FRAME_S)
    return {
        "result": result,
        "wait_ms": (time.perf_counter() - committed_at) * 1000,
        "prepares": prepares,
        "discards": discards,
    }


def pct(values: list[float], share: float) -> float:
    """The ``share`` percentile, nearest rank."""
    return sorted(values)[max(0, round(share * len(values)) - 1)]


def summary(rows: list[dict[str, Any]]) -> None:
    """Print the numbers the ADR cites, then the turns the hybrid heard differently."""
    extra = [float(r["extra_ms"]) for r in rows]
    waits = [float(r["wait_ms"]) for r in rows]
    heard = [float(r["extra_ms"]) for r in rows if r["whisper"]]
    passes = [p for r in rows for p in r["whisper_ms"]]
    print(
        f"\nturns replayed: {len(rows)}; sent to Whisper: {len(heard)}; "
        f"under 1 s of speech: {sum(r['speech_s'] < 1.0 for r in rows)}",
    )
    print(
        f"whisper passes run: {len(passes)} (median {statistics.median(passes):.0f} ms, "
        f"p90 {pct(passes, 0.9):.0f} ms); prepares: {sum(r['prepares'] for r in rows)}, "
        f"discards: {sum(r['discards'] for r in rows)}",
    )
    print(
        f"extra wait beyond SenseVoice alone, all turns: median {statistics.median(extra):.0f} ms, "
        f"p90 {pct(extra, 0.9):.0f} ms, max {max(extra):.0f} ms; "
        f"turns with extra > 0: {sum(e > 0 for e in extra)}",
    )
    if heard:
        print(
            f"  turns sent to Whisper: median {statistics.median(heard):.0f} ms, "
            f"p90 {pct(heard, 0.9):.0f} ms, max {max(heard):.0f} ms",
        )
    print(
        f"wait after the endpoint, hybrid: median {statistics.median(waits):.0f} ms, "
        f"p90 {pct(waits, 0.9):.0f} ms, max {max(waits):.0f} ms",
    )
    differing = [r for r in rows if r["hybrid"] != r["sensevoice"]]
    print(f"\nhybrid differs from SenseVoice on {len(differing)} of {len(rows)} turns:")
    for r in differing:
        print(f"  {r['turn']} ({r['speech_s']:.1f}s, {r['language']})")
        print(f"    sensevoice: {r['sensevoice']}\n    hybrid:     {r['hybrid']}")


def main() -> None:
    """Replay the turns and print the report."""
    parser = argparse.ArgumentParser(description="Replay recorded turns through the hybrid ASR")
    parser.add_argument("--ids", type=Path, help="JSON rows with a turn_id to replay")
    parser.add_argument("--count", type=int, default=80)
    parser.add_argument("--out", type=Path, help="write the per-turn rows as JSON")
    args = parser.parse_args()

    sensevoice = voice_asr.SenseVoiceRecognizer(model_dir=MODELS / "sensevoice-small-int8")
    zh, en = whisper_ears(language="zh"), whisper_ears(language="en")
    if zh is None or en is None:
        sys.exit("mlx-whisper is not installed")
    passes: list[float] = []  # milliseconds of every Whisper pass, wasted ones included
    for whisper in (zh, en):
        inner = whisper.recognize

        def timed(audio: bytes, inner: Any = inner) -> voice_asr.TranscriptionResult:  # noqa: ANN401
            started = time.perf_counter()
            try:
                result: voice_asr.TranscriptionResult = inner(audio)
            finally:
                passes.append((time.perf_counter() - started) * 1000)
            return result

        whisper.recognize = timed  # type: ignore[method-assign, assignment]
    ears = voice_asr.HybridFinalRecognizer(sensevoice=sensevoice, whisper_zh=zh, whisper_en=en)
    vad = voice_audio.SileroVad(mode="record", model_path=MODELS / "silero_vad.onnx")
    ears.prewarm()

    turns = select(args.ids, args.count)
    print(f"{len(turns)} turns decoded; replaying in real time", flush=True)
    rows: list[dict[str, Any]] = []
    for turn, pcm in turns:
        flags, first = speech_flags(vad, pcm)
        if first is None:
            print(f"{turn}: no speech, skipped", flush=True)
            continue
        events = plan(flags, first, vad.endpoint_silence_frames)
        commit_frame, _, speech_frames = events[-1]
        started = time.perf_counter()
        baseline = sensevoice.recognize(pcm[: (commit_frame + 1) * FRAME_BYTES])
        sv_ms = (time.perf_counter() - started) * 1000  # SenseVoice alone, from the same endpoint
        passes.clear()
        done = replay(ears, turn, pcm, first, events)
        result: voice_asr.TranscriptionResult = done["result"]
        row = {
            "turn": turn,
            "speech_s": round(speech_frames * FRAME_S, 3),
            "language": baseline.language_detected,
            "sensevoice_ms": round(sv_ms),
            "wait_ms": round(done["wait_ms"]),
            "extra_ms": round(max(0.0, done["wait_ms"] - sv_ms)),
            "prepares": done["prepares"],
            "discards": done["discards"],
            "whisper_ms": [round(p) for p in passes],
            # SenseVoice's confidence is 0.9 or 0.1; Whisper's is a log-probability mean.
            "whisper": result.confidence not in {0.1, 0.9},
            "sensevoice": baseline.text,
            "hybrid": result.text,
        }
        rows.append(row)
        print(
            f"{turn} speech {row['speech_s']:.2f}s {row['language']} whisper={row['whisper']} "
            f"wait {row['wait_ms']} ms (sensevoice {row['sensevoice_ms']} ms) "
            f"prepares {row['prepares']} discards {row['discards']}",
            flush=True,
        )
    if args.out:
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    summary(rows)


if __name__ == "__main__":
    main()
