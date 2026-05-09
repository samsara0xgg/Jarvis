"""Background wake-word listener that feeds one voice turn into Inherent mode."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

VoiceEvent = Callable[[str, dict[str, Any]], None]


def inherent_wake_enabled(config: Any) -> bool:
    """Return whether the web backend should run the Inherent wake listener."""
    if not isinstance(config, dict):
        return False
    wake_config = config.get("wake_word", {})
    if not isinstance(wake_config, dict):
        return False
    if not bool(wake_config.get("enabled", False)):
        return False
    return bool(wake_config.get("inherent_enabled", True))


class InherentWakeListener:
    """Run openwakeword in the web backend and submit one utterance to Inherent.

    The existing CLI always-listening mode already owns the audio policy:
    keep a lightweight wake stream open, stop it on detection, record one
    utterance through ``AudioRecorder``, then resume wake listening. This class
    adapts that policy for the desktop Inherent card and emits small UI-state
    events over the supplied callback.
    """

    def __init__(self, jarvis_app: Any, emit_voice: VoiceEvent) -> None:
        self.jarvis_app = jarvis_app
        self.emit_voice = emit_voice
        config = getattr(jarvis_app, "config", {})
        self.config = config if isinstance(config, dict) else {}
        session_config = self.config.get("session", {})
        if not isinstance(session_config, dict):
            session_config = {}
        self.utterance_duration = float(session_config.get("utterance_duration", 5))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._submit_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="jarvis-inherent-submit",
        )

    def start(self) -> None:
        """Start the background listener once."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="jarvis-inherent-wake",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Request shutdown and wait briefly for the listener thread."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._submit_executor.shutdown(wait=False, cancel_futures=True)

    def _emit(self, phase: str, **payload: Any) -> None:
        try:
            self.emit_voice(phase, payload)
        except Exception:
            LOGGER.debug("[inherent-wake] voice event emit failed", exc_info=True)

    def _run(self) -> None:
        try:
            import sounddevice as sd
            from core.wake_word import WakeWordDetector
        except Exception as exc:
            LOGGER.warning("[inherent-wake] disabled; dependency unavailable: %s", exc)
            return

        detector = WakeWordDetector(self.config)
        stream = None
        try:
            detector.start()
            stream = self._open_wake_stream(sd, detector)
            LOGGER.info("[inherent-wake] listener started")

            while not self._stop.is_set():
                frame, _ = stream.read(detector.frame_length)
                pcm = frame[:, 0].tolist()
                if not detector.process_frame(pcm):
                    continue

                LOGGER.info("[inherent-wake] wake word detected")
                self._emit("listening")
                try:
                    self._close_wake_stream(stream)
                    stream = None
                    self._capture_and_submit_one_turn()
                finally:
                    detector.reset()
                    if not self._stop.is_set():
                        LOGGER.info("[inherent-wake] reopening wake listener")
                        stream = self._open_wake_stream(sd, detector)
                        LOGGER.info("[inherent-wake] listener reopened")
        except Exception:
            LOGGER.exception("[inherent-wake] listener stopped after error")
            self._emit("error", reason="wake_listener")
        finally:
            try:
                if stream is not None:
                    stream.stop()
                    stream.close()
            except Exception:
                LOGGER.debug("[inherent-wake] stream close failed", exc_info=True)
            detector.stop()

    def _open_wake_stream(self, sd: Any, detector: Any) -> Any:
        stream = sd.InputStream(
            samplerate=detector.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=detector.frame_length,
        )
        stream.start()
        return stream

    def _close_wake_stream(self, stream: Any) -> None:
        try:
            stream.stop()
        except Exception:
            LOGGER.debug("[inherent-wake] wake stream stop failed", exc_info=True)
        try:
            stream.close()
        except Exception:
            LOGGER.debug("[inherent-wake] wake stream close failed", exc_info=True)

    def _capture_and_submit_one_turn(self) -> None:
        try:
            audio = self.jarvis_app.audio_recorder.record(self.utterance_duration)
            if self._stop.is_set():
                return

            self._emit("transcribing")
            result = self.jarvis_app.speech_recognizer.transcribe(np.copy(audio))
            text = (getattr(result, "text", "") or "").strip()
            language = getattr(result, "language", "") or ""
            emotion = getattr(result, "emotion", "") or ""
            if language != "zh" and len(text) <= 5:
                LOGGER.info(
                    "[inherent-wake] dropped non-zh short fragment: lang=%s text=%r",
                    language,
                    text,
                )
                text = ""
            if not text:
                LOGGER.info("[inherent-wake] empty transcript")
                self._emit("empty")
                return

            self._emit("accepted", text=text, emotion=emotion)
            LOGGER.info("[inherent-wake] submit text=%r", text[:80])
            self._submit_executor.submit(self._submit_text, text)
        except Exception:
            LOGGER.exception("[inherent-wake] capture/submit failed")
            self._emit("error", reason="capture")

    def _submit_text(self, text: str) -> None:
        try:
            self.jarvis_app.handle_text(text, session_id="_inherent")
        except Exception:
            LOGGER.exception("[inherent-wake] handle_text failed")
            self._emit("error", reason="submit")
