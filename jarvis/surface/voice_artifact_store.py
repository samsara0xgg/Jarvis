"""L5 voice artifact store — opt-in raw WAV retention (ADR-0005 §4.2).

Per spec §3.6.2: raw ASR audio MAY be retained as an artifact_ref for
debug. Default disabled (privacy preserving); enabled by setting
``JARVIS_VOICE_RETAIN_RAW=1``. The returned path is suitable for the
``audio_artifact_ref`` field on ``utterance.received`` events.
"""
from __future__ import annotations

import os
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def persist(
    pcm_audio: bytes,
    *,
    turn_id: str,
    sample_rate_hz: int,
    artifacts_dir: Path,
) -> str | None:
    """Write ``pcm_audio`` as a mono PCM16 WAV under ``artifacts_dir``.

    Args:
        pcm_audio: raw PCM16 little-endian mono bytes.
        turn_id: filename stem (``{turn_id}.wav``).
        sample_rate_hz: typically 16000 for SenseVoice / Whisper input.
        artifacts_dir: directory to write under.

    Returns:
        Absolute path to the WAV file as a string, or ``None`` when
        retention is disabled.
    """
    if os.environ.get("JARVIS_VOICE_RETAIN_RAW", "0") != "1":
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    out = artifacts_dir / f"{turn_id}.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate_hz)
        w.writeframes(pcm_audio)
    return str(out)


__all__ = ["persist"]
