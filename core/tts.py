"""Text-to-speech engine with Azure Neural TTS, Edge TTS, and pyttsx3 fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# SenseVoice emotion → Azure TTS style mapping
_EMOTION_TO_STYLE = {
    "HAPPY": "cheerful",
    "SAD": "sad",
    "ANGRY": "angry",
    "NEUTRAL": "chat",
    "FEARFUL": "fearful",
    "DISGUSTED": "disgruntled",
    "SURPRISED": "excited",
    "EMO_UNKNOWN": "chat",
}


class TTSEngine:
    """Speak text aloud using Azure Neural TTS, Edge TTS, or pyttsx3.

    Engines (in priority order):
      - ``azure``: Azure Neural TTS with emotion/style control via SSML.
      - ``edge-tts``: Free Microsoft neural voices, no emotion.
      - ``pyttsx3``: Offline fallback, robotic.

    Args:
        config: Parsed application configuration.
    """

    def __init__(self, config: dict) -> None:
        tts_config = config.get("tts", {})
        self.engine_name = str(tts_config.get("engine", "edge-tts")).strip().lower()
        self.edge_voice = str(tts_config.get("edge_voice", "zh-CN-YunxiNeural"))
        self.edge_rate = str(tts_config.get("edge_rate", "+0%"))
        self.fallback_enabled = bool(tts_config.get("fallback_enabled", True))
        self.logger = LOGGER
        self._pyttsx_engine: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")

        # Azure Neural TTS config
        self.azure_key = str(tts_config.get("azure_key", "") or os.environ.get("AZURE_SPEECH_KEY", ""))
        self.azure_region = str(tts_config.get("azure_region", "canadacentral"))
        self.azure_voice = str(tts_config.get("azure_voice", "zh-CN-XiaoxiaoNeural"))
        self._azure_synthesizer: Any = None

    def speak(self, text: str, emotion: str = "") -> None:
        """Speak text aloud with optional emotion.

        Args:
            text: Text to speak.
            emotion: SenseVoice emotion label (e.g. "HAPPY", "SAD"). Only used
                by Azure engine; ignored by Edge TTS and pyttsx3.
        """
        if not text.strip():
            return

        if self.engine_name == "azure":
            try:
                self._speak_azure(text, emotion)
                return
            except Exception as exc:
                self.logger.warning("Azure TTS failed: %s, trying fallback", exc)

        if self.engine_name == "pyttsx3":
            self._speak_pyttsx3(text)
            return

        try:
            self._speak_edge(text)
        except Exception as exc:
            self.logger.warning("Edge TTS failed: %s", exc)
            if self.fallback_enabled:
                try:
                    self._speak_pyttsx3(text)
                except Exception as fallback_exc:
                    self.logger.warning("pyttsx3 fallback also failed: %s", fallback_exc)

    def speak_async(self, text: str) -> None:
        """Speak text in a background thread (non-blocking).

        Fire-and-forget — errors are logged, not raised.
        """
        if not text.strip():
            return
        self._executor.submit(self._speak_safe, text)

    def _speak_safe(self, text: str) -> None:
        """Wrapper that catches all exceptions for background use."""
        try:
            self.speak(text)
        except Exception as exc:
            self.logger.warning("Background TTS failed: %s", exc)

    def speak_short(self, text: str) -> None:
        """Speak a brief acknowledgment with minimal latency.

        Uses pyttsx3 first (no network round-trip) for snappy responses.

        Args:
            text: Short text to speak.
        """
        if not text.strip():
            return
        try:
            self._speak_pyttsx3(text)
        except Exception:
            try:
                self._speak_edge(text)
            except Exception as exc:
                self.logger.warning("All TTS engines failed for short speak: %s", exc)

    def _speak_azure(self, text: str, emotion: str = "") -> None:
        """Generate speech with Azure Neural TTS using SSML for emotion."""
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as exc:
            raise RuntimeError(
                "azure-cognitiveservices-speech is required. "
                "Install with: pip install azure-cognitiveservices-speech"
            ) from exc

        if not self.azure_key:
            raise RuntimeError("Azure Speech key not configured (AZURE_SPEECH_KEY)")

        speech_config = speechsdk.SpeechConfig(
            subscription=self.azure_key, region=self.azure_region,
        )

        # Output to temp file then play (same pattern as edge-tts)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        audio_config = speechsdk.audio.AudioOutputConfig(filename=tmp_path)
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config, audio_config=audio_config,
        )

        # Build SSML with emotion style (escape text for XML safety)
        from xml.sax.saxutils import escape
        style = _EMOTION_TO_STYLE.get(emotion, "chat")
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f'<voice name="{self.azure_voice}">'
            f'<mstts:express-as style="{style}">'
            f'{escape(text)}'
            '</mstts:express-as>'
            '</voice></speak>'
        )

        self.logger.info("Azure TTS: voice=%s style=%s text=%r", self.azure_voice, style, text[:50])
        result = synthesizer.speak_ssml_async(ssml).get()

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            try:
                self._play_audio_file(tmp_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        else:
            Path(tmp_path).unlink(missing_ok=True)
            cancellation = result.cancellation_details
            raise RuntimeError(f"Azure TTS failed: {cancellation.reason} - {cancellation.error_details}")

    def _speak_edge(self, text: str) -> None:
        """Generate speech with edge-tts and play the resulting audio."""
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts is required for Edge TTS. Install with: pip install edge-tts"
            ) from exc

        async def _generate_and_play() -> None:
            communicate = edge_tts.Communicate(
                text, self.edge_voice, rate=self.edge_rate,
            )
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                await communicate.save(tmp_path)
                self._play_audio_file(tmp_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        # Run the async edge-tts call in a fresh or reused event loop
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already inside an async context — run in a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(_generate_and_play())).result()
        else:
            asyncio.run(_generate_and_play())

    def _speak_pyttsx3(self, text: str) -> None:
        """Speak using the offline pyttsx3 engine."""
        try:
            import pyttsx3
        except ImportError as exc:
            raise RuntimeError(
                "pyttsx3 is required for offline TTS. Install with: pip install pyttsx3"
            ) from exc

        if self._pyttsx_engine is None:
            self._pyttsx_engine = pyttsx3.init()
        self._pyttsx_engine.say(text)
        self._pyttsx_engine.runAndWait()

    def _play_audio_file(self, filepath: str) -> None:
        """Play an audio file using the platform's native player."""
        system = platform.system()
        try:
            if system == "Darwin":
                subprocess.run(
                    ["afplay", filepath],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            elif system == "Linux":
                # Try common Linux audio players in order
                for player_cmd in (
                    ["mpv", "--no-video", filepath],
                    ["ffplay", "-nodisp", "-autoexit", filepath],
                    ["aplay", filepath],
                ):
                    try:
                        subprocess.run(
                            player_cmd,
                            check=True,
                            capture_output=True,
                            timeout=30,
                        )
                        return
                    except FileNotFoundError:
                        continue
                self.logger.warning("No audio player found on Linux.")
            elif system == "Windows":
                # Windows — use PowerShell to play
                ps_cmd = (
                    f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'
                )
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
            else:
                self.logger.warning("Unsupported platform for audio playback: %s", system)
        except subprocess.TimeoutExpired:
            self.logger.warning("Audio playback timed out.")
        except subprocess.CalledProcessError as exc:
            self.logger.warning("Audio playback failed: %s", exc)
