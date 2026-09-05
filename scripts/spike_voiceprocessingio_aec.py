"""ADR-0006 D9 spike: score two acoustic captures for VoiceProcessingIO AEC.

Reads the WAV pairs produced by ``scripts/spike_voiceprocessingio_aec.swift``
(one capture with ``inputNode.setVoiceProcessingEnabled(true)``, one without)
and prints the two numbers D9 blocks on that a single sitting can produce:

* **residual echo** — RMS dBFS over the far-end-playback window minus RMS
  dBFS over the lead-in silence window, for each capture, plus the
  AEC-on-minus-AEC-off delta;
* **false candidates** — IDLE->ACTIVE transitions of the real
  :class:`jarvis.surface.voice_audio.SileroVad` over the playback window
  (far-end only, no near-end speaker), once per shipped threshold profile.

Near-end interrupt recall and double-talk recall are *not* measured here:
they need a human talking over playback (ADR-0006 D9). Nothing under
``jarvis/`` is written to; this script only reads the shipped VAD.

Usage::

    PYTHONPATH=. .venv/bin/python scripts/spike_voiceprocessingio_aec.py \
        --aec-off ~/.jarvis-lane-b-test/aec-spike/capture-aec-off.wav \
        --aec-on  ~/.jarvis-lane-b-test/aec-spike/capture-aec-on.wav

Each capture is read together with the ``<capture>.json`` sidecar the Swift
host writes, so the window offsets are measured rather than guessed.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# ``_chunk_db`` is imported rather than copied: the residual figure must be the
# exact dBFS the VAD's own energy gate sees, and a local copy would drift.
from jarvis.surface.voice_audio import (
    SILERO_CHUNK_SAMPLES,
    SileroVad,
    _chunk_db,
)

_SAMPLE_RATE_HZ = 16000
_CHANNELS = 1
_SAMPLE_WIDTH_BYTES = 2
_PROFILES = ("record", "tts")
_SECONDS_PER_MINUTE = 60.0


@dataclass(frozen=True)
class Window:
    """Half-open sample range ``[start, end)`` inside a capture."""

    start: int
    end: int

    @property
    def samples(self) -> int:
        """Number of samples the window spans."""
        return max(0, self.end - self.start)

    @property
    def seconds(self) -> float:
        """Window length in seconds at the fixed 16 kHz capture rate."""
        return self.samples / _SAMPLE_RATE_HZ


@dataclass(frozen=True)
class Capture:
    """One acoustic capture plus the window offsets its host recorded."""

    label: str
    path: Path
    pcm: np.ndarray
    silence: Window
    playback: Window
    facts: dict[str, Any]


@dataclass(frozen=True)
class PeakLevels:
    """Loudest single frame and loudest smoothed run inside a window."""

    peak_frame_dbfs: float
    peak_smoothed_dbfs: float


@dataclass(frozen=True)
class ProfileResult:
    """False-candidate count for one VAD threshold profile over one window."""

    profile: str
    prob_threshold: float
    db_threshold: float
    crossings: int
    window_seconds: float

    @property
    def per_minute(self) -> float:
        """Crossings extrapolated to a per-minute rate."""
        if self.window_seconds <= 0.0:
            return 0.0
        return self.crossings * _SECONDS_PER_MINUTE / self.window_seconds


def read_capture_pcm(path: Path) -> np.ndarray:
    """Read a 16 kHz mono 16-bit WAV as an int16 array.

    The format contract is the one ``FileReplayBackend`` already enforces
    (``jarvis/surface/voice_backend.py``), so a capture that passes here is
    replayable through the real ingress path.

    Args:
        path: WAV file written by the Swift capture host.

    Returns:
        The int16 samples.

    Raises:
        ValueError: If the WAV is not 16 kHz mono 16-bit PCM.
    """
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        if (
            params.framerate != _SAMPLE_RATE_HZ
            or params.nchannels != _CHANNELS
            or params.sampwidth != _SAMPLE_WIDTH_BYTES
        ):
            msg = (
                f"{path}: expected 16000 Hz mono 16-bit PCM, got "
                f"{params.framerate} Hz {params.nchannels}ch "
                f"{params.sampwidth * 8}-bit"
            )
            raise ValueError(msg)
        frames = handle.readframes(params.nframes)
    return np.frombuffer(frames, dtype=np.int16)


def load_capture(label: str, path: Path) -> Capture:
    """Load a capture and the ``<capture>.json`` sidecar written beside it.

    Args:
        label: Human label for the configuration (``aec-off`` / ``aec-on``).
        path: WAV file written by the Swift capture host.

    Returns:
        The capture with its measured windows and format facts.

    Raises:
        FileNotFoundError: If the sidecar is missing.
    """
    sidecar = path.with_suffix(path.suffix + ".json")
    if not sidecar.exists():
        msg = f"{sidecar} missing; it is written by the Swift capture host"
        raise FileNotFoundError(msg)
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    windows = meta["windows"]
    return Capture(
        label=label,
        path=path,
        pcm=read_capture_pcm(path),
        silence=Window(int(windows["silence_start"]), int(windows["silence_end"])),
        playback=Window(int(windows["play_start"]), int(windows["play_end"])),
        facts=dict(meta.get("format_facts", {})),
    )


def rms_dbfs(pcm: np.ndarray) -> float:
    """Return RMS dBFS of an int16 slice using the VAD's own dB formula.

    Args:
        pcm: int16 samples.

    Returns:
        RMS dBFS, floored the same way ``SileroVad`` floors its gate input.
    """
    if pcm.size == 0:
        return float("-inf")
    return _chunk_db(pcm.astype(np.float32) / 32768.0)


def residual_echo_db(capture: Capture) -> float:
    """Playback-window RMS dBFS minus silence-window RMS dBFS.

    Args:
        capture: A loaded capture.

    Returns:
        The residual echo figure D9 asks for, in dB.
    """
    play = rms_dbfs(capture.pcm[capture.playback.start : capture.playback.end])
    silence = rms_dbfs(capture.pcm[capture.silence.start : capture.silence.end])
    return play - silence


def peak_levels(pcm: np.ndarray) -> PeakLevels:
    """Return the loudest 32 ms frame and the loudest 5-frame smoothed run.

    The window RMS a residual figure is built from hides peaks, and it is a
    peak that trips the VAD's energy gate. The smoothing width matches
    ``VadThresholds.smoothing_window``, which is what the gate actually sees.

    Args:
        pcm: int16 samples at 16 kHz; a trailing partial frame is dropped.

    Returns:
        Peak raw-frame and peak smoothed dBFS.
    """
    usable = pcm.size - (pcm.size % SILERO_CHUNK_SAMPLES)
    if usable == 0:
        return PeakLevels(float("-inf"), float("-inf"))
    scaled = pcm[:usable].astype(np.float32) / 32768.0
    frames = [
        _chunk_db(scaled[start : start + SILERO_CHUNK_SAMPLES])
        for start in range(0, usable, SILERO_CHUNK_SAMPLES)
    ]
    window: deque[float] = deque(maxlen=SileroVad.thresholds("record").smoothing_window)
    smoothed = []
    for value in frames:
        window.append(value)
        smoothed.append(float(np.mean(window)))
    return PeakLevels(max(frames), max(smoothed))


def count_vad_crossings(pcm: np.ndarray, *, mode: str, model_path: Path | None) -> int:
    """Count IDLE->ACTIVE transitions of the shipped VAD over ``pcm``.

    A fresh :class:`SileroVad` is built per call and ``prepare_utterance()``
    converges it on silence first, matching how the recorder arms it.

    Args:
        pcm: int16 samples at 16 kHz; a trailing partial frame is dropped.
        mode: ``record`` or ``tts`` — the shipped threshold profiles.
        model_path: Silero ONNX path, forwarded verbatim to :class:`SileroVad`
            (``None`` only works when the ONNX session is patched, which is
            what the hermetic test does).

    Returns:
        Number of rising edges of ``is_speech_detected()``.
    """
    vad = SileroVad(mode=mode, model_path=model_path)
    vad.prepare_utterance()
    crossings = 0
    was_active = False
    usable = pcm.size - (pcm.size % SILERO_CHUNK_SAMPLES)
    for start in range(0, usable, SILERO_CHUNK_SAMPLES):
        vad.feed(pcm[start : start + SILERO_CHUNK_SAMPLES].tobytes())
        active = vad.is_speech_detected()
        if active and not was_active:
            crossings += 1
        was_active = active
    return crossings


def profile_results(capture: Capture, *, model_path: Path | None) -> list[ProfileResult]:
    """Score both shipped threshold profiles over the playback window.

    Args:
        capture: A loaded capture.
        model_path: Silero ONNX path, forwarded verbatim to :class:`SileroVad`.

    Returns:
        One :class:`ProfileResult` per profile, in ``record``/``tts`` order.
    """
    window = capture.pcm[capture.playback.start : capture.playback.end]
    results = []
    for profile in _PROFILES:
        thresholds = SileroVad.thresholds(profile)
        results.append(
            ProfileResult(
                profile=profile,
                prob_threshold=thresholds.prob_threshold,
                db_threshold=thresholds.db_threshold,
                crossings=count_vad_crossings(window, mode=profile, model_path=model_path),
                window_seconds=capture.playback.seconds,
            )
        )
    return results


def _write_capture_report(capture: Capture, results: list[ProfileResult]) -> None:
    """Print one capture's residual, false candidates and format facts."""
    out = sys.stdout.write
    out(f"== {capture.label} ({capture.path})\n")
    out(
        f"  windows: silence [{capture.silence.start}, {capture.silence.end}) "
        f"{capture.silence.seconds:.2f}s · playback "
        f"[{capture.playback.start}, {capture.playback.end}) "
        f"{capture.playback.seconds:.2f}s\n"
    )
    play = rms_dbfs(capture.pcm[capture.playback.start : capture.playback.end])
    silence = rms_dbfs(capture.pcm[capture.silence.start : capture.silence.end])
    out(
        f"  rms dBFS: playback {play:+.2f} · silence {silence:+.2f} · "
        f"residual echo {residual_echo_db(capture):+.2f} dB\n"
    )
    peaks = peak_levels(capture.pcm[capture.playback.start : capture.playback.end])
    out(
        f"  playback peaks dBFS: loudest frame {peaks.peak_frame_dbfs:+.2f} · "
        f"loudest 5-frame smoothed {peaks.peak_smoothed_dbfs:+.2f}\n"
    )
    for result in results:
        out(
            f"  false candidates [{result.profile}: prob>={result.prob_threshold} "
            f"dB>={result.db_threshold}]: {result.crossings} over "
            f"{result.window_seconds:.2f}s = {result.per_minute:.2f}/min\n"
        )
    for key in sorted(capture.facts):
        out(f"  fact {key}: {json.dumps(capture.facts[key], ensure_ascii=False)}\n")


def main(argv: list[str] | None = None) -> int:
    """Score both captures and print the D9 numbers.

    Args:
        argv: Command line, or ``None`` for ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description="ADR-0006 D9 AEC spike analysis")
    parser.add_argument("--aec-off", type=Path, required=True)
    parser.add_argument("--aec-on", type=Path, required=True)
    parser.add_argument("--silero", type=Path, default=Path("data/silero_vad.onnx"))
    args = parser.parse_args(argv)

    captures = [
        load_capture("aec-off", args.aec_off),
        load_capture("aec-on", args.aec_on),
    ]
    residuals = {}
    for capture in captures:
        results = profile_results(capture, model_path=args.silero)
        _write_capture_report(capture, results)
        residuals[capture.label] = residual_echo_db(capture)
    delta = residuals["aec-on"] - residuals["aec-off"]
    sys.stdout.write(
        f"== residual echo delta (aec-on minus aec-off): {delta:+.2f} dB\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
