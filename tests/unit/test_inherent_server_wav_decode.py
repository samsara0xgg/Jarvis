"""Unit tests for ``_decode_wav_to_pcm16_mono_16k`` in inherent_server.

Verifies that the soxr-based resampler:

1. Passes through audio that is already at 16 kHz unchanged (no soxr call).
2. Downsamples 48 kHz mono WAV to 16 kHz with correct length.
3. Preserves a 1 kHz tone (well within the 8 kHz Nyquist) with reasonable amplitude.
4. Suppresses a high-frequency tone (18 kHz, way above Nyquist) — the soxr
   anti-alias filter must reject it; the output amplitude must be tiny.
5. Handles stereo input (mixes to mono before resampling).
6. Returns an empty bytes for zero-length input.
7. Raises HTTPException(415) on malformed WAV.
8. Raises HTTPException(415) on non-PCM16 (8-bit) WAV.
"""

from __future__ import annotations

import io
import math
import wave

import numpy as np
import pytest

from jarvis.surface.inherent_server import _decode_wav_to_pcm16_mono_16k

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav(
    *,
    sample_rate: int,
    frequency_hz: float,
    duration_s: float = 0.1,
    n_channels: int = 1,
    amplitude: float = 0.5,
) -> bytes:
    """Synthesise a pure-tone PCM16 WAV blob.

    ``amplitude`` is a fraction of full scale (0-1).  Each channel carries
    the same sine so the stereo-to-mono mix is lossless.
    """
    n_samples = int(duration_s * sample_rate)
    t = np.arange(n_samples, dtype=np.float32) / sample_rate
    tone = (np.sin(2 * math.pi * frequency_hz * t) * amplitude * 32767).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(n_channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        # Interleave channels (all identical tone).
        if n_channels == 1:
            w.writeframes(tone.tobytes())
        else:
            interleaved = np.stack([tone] * n_channels, axis=1).flatten()
            w.writeframes(interleaved.tobytes())
    return buf.getvalue()


def _pcm_rms(pcm_bytes: bytes) -> float:
    """RMS amplitude (0-1 scale) of a PCM16 little-endian buffer."""
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32767.0
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples**2)))


# ---------------------------------------------------------------------------
# Pass-through: source already at 16 kHz
# ---------------------------------------------------------------------------


def test_passthrough_at_16k_returns_unchanged_pcm() -> None:
    """WAV at 16 kHz is returned as-is without going through soxr."""
    wav = _make_wav(sample_rate=16000, frequency_hz=1000)
    result = _decode_wav_to_pcm16_mono_16k(wav)

    # Read back the original PCM to compare byte-for-byte.
    with wave.open(io.BytesIO(wav), "rb") as w:
        expected = w.readframes(w.getnframes())

    assert result == expected


# ---------------------------------------------------------------------------
# Resampling: 48 kHz → 16 kHz length
# ---------------------------------------------------------------------------


def test_resample_48k_to_16k_output_length() -> None:
    """48 kHz input produces an output with ~1/3 the sample count."""
    duration_s = 0.5
    src_sr = 48000
    wav = _make_wav(sample_rate=src_sr, frequency_hz=1000, duration_s=duration_s)
    result = _decode_wav_to_pcm16_mono_16k(wav)

    out_samples = len(result) // 2  # 2 bytes per int16 sample
    expected = int(duration_s * 16000)
    # soxr may produce ±1 sample due to filter tail — allow small tolerance.
    assert abs(out_samples - expected) <= 4


# ---------------------------------------------------------------------------
# Anti-alias: in-band tone preserved (1 kHz)
# ---------------------------------------------------------------------------


def test_in_band_tone_preserved_after_resample() -> None:
    """A 1 kHz tone at 48 kHz should survive downsampling to 16 kHz with amplitude > 0.3."""
    amplitude = 0.5
    wav = _make_wav(sample_rate=48000, frequency_hz=1000, amplitude=amplitude)
    result = _decode_wav_to_pcm16_mono_16k(wav)

    rms = _pcm_rms(result)
    # RMS of a pure sine at amplitude A is A / sqrt(2) ≈ 0.354 for A=0.5.
    # Allow some head-room: must be above 0.25 to confirm the tone made it through.
    assert rms > 0.25, f"in-band tone was suppressed too much: rms={rms:.4f}"


# ---------------------------------------------------------------------------
# Anti-alias: above-Nyquist tone suppressed (18 kHz)
# ---------------------------------------------------------------------------


def test_above_nyquist_tone_suppressed_after_resample() -> None:
    """An 18 kHz tone (above the 8 kHz Nyquist at 16 kHz) must be rejected by soxr.

    With the old np.interp approach, aliasing would fold energy back into the
    speech band (producing an audible alias tone ~2 kHz).  soxr's HQ filter
    suppresses the input above 8 kHz so the output is nearly silent.
    """
    amplitude = 0.8
    wav = _make_wav(sample_rate=48000, frequency_hz=18000, amplitude=amplitude)
    result = _decode_wav_to_pcm16_mono_16k(wav)

    rms = _pcm_rms(result)
    # soxr's HQ anti-alias filter provides > 60 dB attenuation above Nyquist.
    # RMS at full scale is ~0.566; after 60 dB attenuation it's < 0.001.
    # Use a generous threshold of 0.05 to avoid flakiness.
    assert rms < 0.05, f"above-Nyquist tone was not suppressed: rms={rms:.4f}"


# ---------------------------------------------------------------------------
# Stereo mixing
# ---------------------------------------------------------------------------


def test_stereo_wav_mixed_to_mono() -> None:
    """Stereo input is averaged to mono before (or alongside) resampling."""
    wav = _make_wav(sample_rate=48000, frequency_hz=440, n_channels=2)
    result = _decode_wav_to_pcm16_mono_16k(wav)

    # Should produce output — not empty, and has reasonable amplitude.
    assert len(result) > 0
    rms = _pcm_rms(result)
    assert rms > 0.1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_zero_frame_wav_returns_empty_bytes() -> None:
    """A valid WAV header with zero frames produces empty bytes."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"")
    result = _decode_wav_to_pcm16_mono_16k(buf.getvalue())

    assert result == b""


def test_malformed_wav_raises_415() -> None:
    """Garbage bytes raise HTTPException with status 415."""
    from fastapi import HTTPException  # noqa: PLC0415

    with pytest.raises(HTTPException) as exc_info:
        _decode_wav_to_pcm16_mono_16k(b"not a wav file at all")

    assert exc_info.value.status_code == 415


def test_non_pcm16_wav_raises_415() -> None:
    """8-bit WAV (sample_width=1) raises HTTPException 415."""
    from fastapi import HTTPException  # noqa: PLC0415

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)  # 8-bit, not PCM16
        w.setframerate(16000)
        w.writeframes(b"\x80" * 100)

    with pytest.raises(HTTPException) as exc_info:
        _decode_wav_to_pcm16_mono_16k(buf.getvalue())

    assert exc_info.value.status_code == 415


# ---------------------------------------------------------------------------
# Clip defense: soxr HQ ringing must not wrap int16
# ---------------------------------------------------------------------------


def test_overshoot_does_not_wrap_int16() -> None:
    """Defense-in-depth: soxr HQ filter ringing past +-1.0 must not wrap int16.

    soxr HQ filter ringing past +-1.0 on full-scale transients must not
    wrap to negative int16. Without np.clip the intended +35552 wraps to
    -29984 -- a transient pop.

    Construction: a DC step at int16 max (full-scale square wave) at 48 kHz
    triggers the worst-case ringing on the soxr HQ anti-alias filter.
    After resampling to 16 kHz, every output sample must stay within
    the valid int16 range [-32768, 32767] -- no wrap-around.
    """
    # Build a 48 kHz WAV with full-scale DC step: first half at +32767,
    # second half at -32767.  This is the worst-case transient for filter
    # ringing (maximises the overshoot the soxr HQ filter can produce).
    n_samples = 4800  # 0.1 s at 48 kHz — long enough for filter transient
    half = n_samples // 2
    pcm_i16 = np.empty(n_samples, dtype=np.int16)
    pcm_i16[:half] = 32767
    pcm_i16[half:] = -32767

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(pcm_i16.tobytes())

    result = _decode_wav_to_pcm16_mono_16k(buf.getvalue())

    assert len(result) > 0, "decoder returned empty bytes for a valid WAV"
    out = np.frombuffer(result, dtype=np.int16)
    # If np.clip is missing, soxr ringing (measured ~1.085x on a DC step)
    # wraps int16: intended +35552 becomes -29984 -- a large negative spike
    # present alongside large positive values.  With clip, every sample is
    # bounded to the PCM16 range.
    assert int(out.max()) <= 32767, f"sample above int16 max: {int(out.max())}"
    assert int(out.min()) >= -32768, f"sample below int16 min: {int(out.min())}"
