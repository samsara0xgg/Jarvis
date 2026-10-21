"""Provider PCM to the player's canonical rate, and the tones played when a voice breaks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from jarvis.surface.tts_silence import SilenceTrimmer


def cut_off_cue(rate_hz: int) -> bytes:
    """Two soft falling notes (E5, A4), 90 ms each, as canonical float32 PCM."""
    notes = []
    for frequency in (659.25, 440.0):
        t = np.arange(int(rate_hz * 0.09)) / rate_hz
        envelope = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.012)  # 12 ms ramps
        notes.append(0.18 * envelope * np.sin(2 * np.pi * frequency * t))
        notes.append(np.zeros(int(rate_hz * 0.03)))
    return np.concatenate(notes).astype("<f4").tobytes()


def head_dropped(trimmer: SilenceTrimmer | None) -> int:
    """Return the samples the trimmer dropped from the head, or 0 without one."""
    return 0 if trimmer is None else trimmer.head_dropped


class SegmentResampler:
    """Independent int16-LE provider segment to canonical float32-LE PCM."""

    def __init__(self, *, input_rate_hz: int, output_rate_hz: int) -> None:
        """Resample from ``input_rate_hz`` to ``output_rate_hz`` (no resampler when equal)."""
        self._input_rate_hz = input_rate_hz
        self._output_rate_hz = output_rate_hz
        self._carry = b""
        self._closed = False
        self._resampler: Any | None = None
        if input_rate_hz != output_rate_hz:
            import soxr  # noqa: PLC0415

            self._resampler = soxr.ResampleStream(
                input_rate_hz,
                output_rate_hz,
                1,
                dtype="float32",
                quality="HQ",
            )

    def feed(self, pcm: bytes) -> bytes:
        """Decode and incrementally resample one provider chunk."""
        if self._closed:
            msg = "cannot feed a finalized segment resampler"
            raise RuntimeError(msg)
        raw = self._carry + pcm
        aligned = len(raw) - (len(raw) % 2)
        self._carry = raw[aligned:]
        if aligned == 0:
            return b""
        samples = np.frombuffer(raw[:aligned], dtype="<i2").astype(np.float32)
        samples /= 32768.0
        if self._resampler is not None:
            samples = self._resampler.resample_chunk(samples)
        return samples.astype(np.float32, copy=False).tobytes()

    def finish(self) -> bytes:
        """Finalize only after TTSSegmentFinished; discard one impossible odd byte."""
        if self._closed:
            return b""
        self._closed = True
        self._carry = b""
        if self._resampler is None:
            return b""
        tail = self._resampler.resample_chunk(
            np.zeros(0, dtype=np.float32),
            last=True,
        )
        return np.asarray(tail, dtype=np.float32).tobytes()
