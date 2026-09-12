"""L5 voice artifact store — per-utterance audio retention for memory.db.

Every recognised utterance is written under ``artifacts_dir`` (the
``memory.audio_dir`` config value) and transcoded to AAC with macOS's
built-in ``afconvert``; when that fails the PCM16 WAV stays. The returned
path lands on ``utterance.received.audio_artifact_ref`` and from there on
``records.audio_path``. ``artifacts_dir=None`` disables retention.
"""

from __future__ import annotations

import subprocess
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_AAC_BITRATE: str = "32000"
_TRANSCODE_TIMEOUT_S: float = 30.0


def persist(
    pcm_audio: bytes,
    *,
    turn_id: str,
    sample_rate_hz: int,
    artifacts_dir: Path | None,
) -> str | None:
    """Write ``pcm_audio`` (mono PCM16 LE) under ``artifacts_dir``; return its path.

    Returns ``None`` when retention is disabled. The file is
    ``{turn_id}.m4a`` after a successful AAC transcode, else ``{turn_id}.wav``.
    """
    if artifacts_dir is None:
        return None
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    wav_path = artifacts_dir / f"{turn_id}.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate_hz)
        w.writeframes(pcm_audio)
    m4a_path = wav_path.with_suffix(".m4a")
    argv = [
        "afconvert",
        "-f",
        "m4af",
        "-d",
        "aac",
        "-b",
        _AAC_BITRATE,
        str(wav_path),
        str(m4a_path),
    ]
    try:
        # S603: fixed argv, no shell.
        subprocess.run(argv, check=True, capture_output=True, timeout=_TRANSCODE_TIMEOUT_S)  # noqa: S603
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # ponytail: no encoder → keep WAV (~21 GB/yr at 30 min/day); AAC is ~2.6 GB/yr.
        return str(wav_path)
    wav_path.unlink(missing_ok=True)
    return str(m4a_path)


__all__ = ["persist"]
