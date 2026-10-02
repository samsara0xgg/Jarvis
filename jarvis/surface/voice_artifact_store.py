"""L5 voice artifact store — per-utterance audio retention for memory.db.

Every recognised utterance is written under ``artifacts_dir`` (the
``memory.audio_dir`` config value) and transcoded to AAC with macOS's
built-in ``afconvert``; when that fails the PCM16 WAV stays. The returned
path lands on ``utterance.received.audio_artifact_ref`` and from there on
``records.audio_path``. ``artifacts_dir=None`` disables retention.
"""

from __future__ import annotations

import json
import logging
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

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


class TtsRecorder:
    """Test-time copy of what MiniMax returned for each segment (ADR 0120).

    ``tts-<response_id>-<sequence>.m4a`` (or ``.wav``) and a one-line
    ``.json`` of the text sent, voice, model and rate, in the recordings
    folder, so the retention of ADR 0067 deletes them with the input audio.
    The write runs on a thread of its own after the segment ends; a failure
    costs one warning, never playback.
    """

    def __init__(self, folder: Path) -> None:
        """Keep recordings under ``folder`` (``memory.audio_dir``)."""
        self._folder = folder
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-tts-keep")
        self._warned = False

    def keep(  # noqa: PLR0913 - one segment's audio and the note that goes beside it
        self, response_id: str, sequence: int, pcm: bytes, *, sample_rate_hz: int,
        text: str, voice: str, model: str, complete: bool = True,
    ) -> None:
        """Queue ``pcm`` (mono PCM16 LE, as the provider sent it) to be written."""
        if pcm:
            self._pool.submit(
                self._save, f"tts-{response_id}-{sequence}", pcm, sample_rate_hz,
                {"response_id": response_id, "sequence": sequence, "text": text,
                 "voice": voice, "model": model, "complete": complete},
            )

    def _save(self, name: str, pcm: bytes, sample_rate_hz: int, note: dict[str, object]) -> None:
        try:
            path = persist(
                pcm, turn_id=name, sample_rate_hz=sample_rate_hz, artifacts_dir=self._folder,
            )
            note = {
                "saved": datetime.now().astimezone().isoformat(timespec="seconds"),
                "seconds": round(len(pcm) / 2 / sample_rate_hz, 2),
                "sample_rate_hz": sample_rate_hz,
                "audio": None if path is None else Path(path).name,
                **note,
            }
            (self._folder / f"{name}.json").write_text(
                json.dumps(note, ensure_ascii=False) + "\n", encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - a diagnostic copy must never break speech
            if not self._warned:
                self._warned = True
                LOGGER.warning("tts recording not kept: %s", exc)


__all__ = ["TtsRecorder", "persist"]
