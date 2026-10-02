"""Test-daemon-only patches for the voice harness (loaded through PYTHONPATH by ``run.py``).

Active only in the process whose parent is the runner (``JARVIS_VOICE_HARNESS_PARENT``), so the
live daemon, the runner itself and any child of the test daemon stay untouched. Four patches:

* ``SoundDeviceDuplexBackend`` becomes a spool backend: silence at real-time pace, and each
  ``<spool>/<name>.wav`` that appears is played through the real ingress, then renamed
  ``<name>.done``. ``<name>.play.json`` records when its first sample was "heard".
* The final recognizer returns the text of ``<name>.script.json`` for utterances that came from
  that wav (pieces matched by audio fingerprint) and the real SenseVoice otherwise.
* ``SystemAudioDucker`` never runs osascript: system volume is not touched.
* MiniMax ``task_start`` / ``task_continue`` messages are logged (language_boost per text).
"""

# ruff: noqa: C901

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Any

_CJK = re.compile(r"[㐀-鿿]")
_FP_CHUNK = 1600  # samples: 0.1 s fingerprint windows
_FP_STEP = 4800  # samples: one window per 0.3 s of a piece
_MATCH_FRACTION = 0.5
_SILENT_RMS = 300.0
_RATE = 16_000


def _install(root: Path) -> None:
    import numpy as np  # noqa: PLC0415 - only inside the test daemon

    from jarvis.surface import (  # noqa: PLC0415
        voice_asr,
        voice_backend,
        voice_ducking,
        voice_pipeline,
        voice_tts,
    )

    spool = root / "spool"
    log_dir = root / "harness"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_lock = threading.Lock()
    pieces: list[tuple[str | None, list[bytes]]] = []

    def log(name: str, **fields: object) -> None:
        line = json.dumps({"t_wall": time.time(), **fields}, ensure_ascii=False)
        with log_lock, (log_dir / name).open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def fingerprints(pcm: bytes, start: int, end: int) -> list[bytes]:
        samples = np.frombuffer(pcm, dtype="<i2")
        chunks = []
        for at in range(start, max(start + 1, end - _FP_CHUNK), _FP_STEP):
            window = samples[at : at + _FP_CHUNK]
            if (
                len(window) == _FP_CHUNK
                and float(np.sqrt(np.mean(window.astype(float) ** 2))) > _SILENT_RMS
            ):
                chunks.append(window.tobytes())
        return chunks

    class SpoolBackend(voice_backend.FileReplayBackend):
        """Real-time paced silence that plays each spooled wav through the real ingress."""

        def __init__(self, *, input_format: Any, **_ignored: object) -> None:  # noqa: ANN401
            if input_format.channels != 1:
                msg = "the voice harness needs realtime.single_audio_ingress.wake_input_channel: 0"
                raise ValueError(msg)
            self._path = spool
            self._frame_samples = input_format.callback_frame_samples
            self._tail_silence_s = None
            self._format = voice_backend.AudioInputFormat(
                sample_rate_hz=_RATE,
                channels=1,
                callback_frame_samples=self._frame_samples,
            )
            self._lock = threading.Lock()
            self._stop = threading.Event()
            self._thread = None
            self._epoch = None
            self._attempt_id = None
            self._version = 0
            self.frames_emitted = 0
            self.eof_reached = False

        def _next_wav(self) -> Path | None:
            ready = sorted(spool.glob("*.wav")) if spool.is_dir() else []
            return ready[0] if ready else None

        def _load(self, wav_path: Path, t0_mono: float) -> tuple[bytes, dict[str, Any]]:
            with wave.open(str(wav_path), "rb") as reader:
                if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (
                    _RATE,
                    1,
                    2,
                ):
                    msg = f"{wav_path.name}: spool wav must be 16 kHz mono PCM16"
                    raise ValueError(msg)
                pcm = reader.readframes(reader.getnframes())
            script = wav_path.with_suffix(".script.json")
            if script.exists():
                pieces.extend(
                    (piece["text"], fingerprints(pcm, piece["start"], piece["end"]))
                    for piece in json.loads(script.read_text(encoding="utf-8"))["pieces"]
                )
            meta = {
                "wav": wav_path.name,
                "duration_s": len(pcm) / 2 / _RATE,
                "t0_mono": t0_mono,
                "t0_wall": time.time() + (t0_mono - time.monotonic()),
            }
            wav_path.with_suffix(".play.json").write_text(json.dumps(meta), encoding="utf-8")
            return pcm, meta

        def _finish(self, wav_path: Path, meta: dict[str, Any]) -> None:
            meta["end_wall"] = meta["t0_wall"] + meta["duration_s"]
            meta["end_mono"] = meta["t0_mono"] + meta["duration_s"]
            wav_path.with_suffix(".play.json").write_text(json.dumps(meta), encoding="utf-8")
            wav_path.rename(wav_path.with_suffix(".done"))

        def _run(self, stream_epoch: int, attempt_id: str, frame_sink: Any) -> None:  # noqa: ANN401
            frame_bytes = self._frame_samples * 2
            period_s = self._frame_samples / _RATE
            silence = bytes(frame_bytes)
            deadline = time.monotonic()
            pcm = b""
            offset = 0
            wav_path: Path | None = None
            meta: dict[str, Any] = {}
            while not self._stop.is_set():
                if wav_path is not None and offset >= len(pcm):
                    self._finish(wav_path, meta)
                    wav_path = None
                if wav_path is None:
                    wav_path = self._next_wav()
                    if wav_path is not None:
                        try:
                            pcm, meta = self._load(wav_path, deadline)
                        except ValueError:
                            wav_path.rename(wav_path.with_suffix(".bad"))
                            wav_path = None
                        else:
                            offset = 0
                frame = silence
                if wav_path is not None:
                    frame = pcm[offset : offset + frame_bytes]
                    frame += bytes(frame_bytes - len(frame))
                    offset += frame_bytes
                deadline += period_s
                time.sleep(max(0.0, deadline - time.monotonic()))
                if self._stop.is_set():
                    return
                frame_sink(
                    stream_epoch=stream_epoch,
                    attempt_id=attempt_id,
                    callback_buffer=frame,
                    frame_count=self._frame_samples,
                    adc_time_s=None,
                    captured_monotonic_ns=time.monotonic_ns(),
                    discontinuity_before=False,
                )
                self.frames_emitted += 1

    voice_backend.SoundDeviceDuplexBackend = SpoolBackend  # type: ignore[misc, assignment]

    # --- scripted final ASR -------------------------------------------------------------
    def script_for(audio: bytes) -> list[str] | None:
        found: list[tuple[int, str]] = []
        for text, chunks in pieces:
            if text is None or not chunks:
                continue
            hits = [audio.find(chunk) for chunk in chunks]
            if sum(h >= 0 for h in hits) / len(chunks) >= _MATCH_FRACTION:
                found.append((min(h for h in hits if h >= 0), text))
        return [text for _, text in sorted(found)] or None

    class ScriptedRecognizer:
        """Delegates everything to the real recognizer except ``recognize``."""

        def __init__(self, real: Any) -> None:  # noqa: ANN401
            self._real = real

        def __getattr__(self, name: str) -> Any:  # noqa: ANN401
            return getattr(self._real, name)

        def recognize(self, audio_pcm: bytes) -> voice_asr.TranscriptionResult:
            started = time.time()
            texts = script_for(audio_pcm)
            if texts is not None:
                text = " ".join(texts)
                result = voice_asr.TranscriptionResult(
                    text=text,
                    confidence=0.9,
                    language_detected="zh" if _CJK.search(text) else "en",
                    emotion=None,
                )
                mode = "scripted"
            else:
                result = self._real.recognize(audio_pcm)
                mode = "real"
            log(
                "asr.jsonl",
                mode=mode,
                text=result.text,
                audio_s=len(audio_pcm) / 2 / _RATE,
                decode_s=time.time() - started,
                language_detected=result.language_detected,
            )
            return result

    real_init = voice_pipeline.VoicePipeline.__init__

    def pipeline_init(self: Any, *, recognizer: Any, **kwargs: Any) -> None:  # noqa: ANN401
        real_init(self, recognizer=ScriptedRecognizer(recognizer), **kwargs)

    voice_pipeline.VoicePipeline.__init__ = pipeline_init  # type: ignore[method-assign]

    # --- no system volume changes -------------------------------------------------------
    def fake_osascript(script: str) -> str:
        log("ducker.jsonl", script=script.strip().splitlines()[0] if script.strip() else "")
        return "output volume:50, output muted:false"

    voice_ducking._run_osascript = fake_osascript  # noqa: SLF001

    # --- MiniMax request log: the language each text was sent under ---------------------
    real_connect = voice_tts._ws_connect  # noqa: SLF001

    class LoggedConnection:
        def __init__(self, conn: Any) -> None:  # noqa: ANN401
            self._conn = conn
            self._id = f"c{id(conn):x}"

        def __getattr__(self, name: str) -> Any:  # noqa: ANN401
            return getattr(self._conn, name)

        async def send(self, data: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            with contextlib.suppress(Exception):
                message = json.loads(data)
                if message.get("event") == "task_start":
                    log(
                        "tts-requests.jsonl",
                        conn=self._id,
                        event="task_start",
                        language_boost=message.get("language_boost"),
                        voice=message.get("voice_setting", {}).get("voice_id"),
                    )
                elif message.get("event") == "task_continue":
                    log(
                        "tts-requests.jsonl",
                        conn=self._id,
                        event="task_continue",
                        text=message.get("text"),
                    )
            return await self._conn.send(data, *args, **kwargs)

    async def logged_connect(url: str, *, additional_headers: dict[str, str]) -> Any:  # noqa: ANN401
        return LoggedConnection(await real_connect(url, additional_headers=additional_headers))

    voice_tts._ws_connect = logged_connect  # noqa: SLF001


_ROOT = os.environ.get("JARVIS_VOICE_HARNESS_ROOT")
if _ROOT and os.environ.get("JARVIS_VOICE_HARNESS_PARENT") == str(os.getppid()):
    _install(Path(_ROOT))
