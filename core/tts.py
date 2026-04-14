"""Text-to-speech engine with Azure Neural TTS, Edge TTS, and pyttsx3 fallback.

Also provides TTSPipeline — a dual-thread pipeline that decouples TTS synthesis
from audio playback, eliminating inter-sentence pauses during streaming output.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from typing import Any

LOGGER = logging.getLogger(__name__)

# User's detected emotion → Jarvis's RESPONSE style (not echo)
# 用户开心→Jarvis也开心；用户难过→Jarvis温柔安慰；用户生气→Jarvis冷静

# Azure TTS styles
_EMOTION_TO_AZURE_STYLE = {
    "HAPPY": "cheerful",
    "SAD": "gentle",
    "ANGRY": "calm",
    "NEUTRAL": "chat",
    "FEARFUL": "gentle",
    "DISGUSTED": "calm",
    "SURPRISED": "cheerful",
    "EMO_UNKNOWN": "chat",
}

# OpenAI TTS emotion instructions (natural language, most expressive)
_EMOTION_TO_OPENAI_INSTRUCTION = {
    "HAPPY": "语气愉快轻松，像分享好消息的朋友。",
    "SAD": "语气温柔关心，像安慰朋友一样，温暖但不夸张。",
    "ANGRY": "语气平静沉稳，安抚对方情绪，让人感到安心。",
    "NEUTRAL": "",
    "FEARFUL": "语气稳重温暖，给人安全感。",
    "DISGUSTED": "语气理解包容，不否定对方的感受。",
    "SURPRISED": "语气有活力，带着好奇和兴趣。",
    "EMO_UNKNOWN": "",
}

# MiniMax TTS emotions (direct API parameter)
_EMOTION_TO_MINIMAX = {
    "HAPPY": "happy",
    "SAD": "sad",
    "ANGRY": "calm",
    "NEUTRAL": "calm",
    "FEARFUL": "calm",
    "DISGUSTED": "calm",
    "SURPRISED": "happy",
    "EMO_UNKNOWN": "calm",
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

    def __init__(self, config: dict, tracker: Any = None) -> None:
        tts_config = config.get("tts", {})
        self.engine_name = str(tts_config.get("engine", "edge-tts")).strip().lower()
        self.edge_voice = str(tts_config.get("edge_voice", "zh-CN-YunxiNeural"))
        self.edge_rate = str(tts_config.get("edge_rate", "+0%"))
        self.speed = float(tts_config.get("speed", 1.0))
        self.speed = max(0.25, min(4.0, self.speed))
        self.fallback_enabled = bool(tts_config.get("fallback_enabled", True))
        self.logger = LOGGER
        self._tracker = tracker
        self._pyttsx_engine: Any = None
        self._openai_client: Any = None
        self._http_session: Any = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._platform = platform.system()
        self._play_proc: subprocess.Popen | None = None
        self._play_lock = threading.Lock()

        # TTS audio cache for short responses
        self._tts_cache_dir = Path(tts_config.get("cache_dir", "data/cache/tts"))
        self._tts_cache_dir.mkdir(parents=True, exist_ok=True)
        self._tts_cache_max = int(tts_config.get("cache_max_files", 500))
        self._last_cache_hit: bool | None = None  # trace for system tests

        # Lazy-init reusable HTTP session
        try:
            import requests
            self._http_session = requests.Session()
        except ImportError:
            pass

        # Azure Neural TTS config
        self.azure_key = str(tts_config.get("azure_key", "") or os.environ.get("AZURE_SPEECH_KEY", ""))
        self.azure_region = str(tts_config.get("azure_region", "canadacentral"))
        self.azure_voice = str(tts_config.get("azure_voice", "zh-CN-XiaoxiaoNeural"))
        self._azure_synthesizer: Any = None

        # MiniMax TTS config
        self.minimax_key = str(tts_config.get("minimax_key", "") or os.environ.get("MINIMAX_API_KEY", ""))
        self.minimax_model = str(tts_config.get("minimax_model", "speech-02-turbo"))
        self.minimax_voice = str(tts_config.get("minimax_voice", "male-qn-qingse"))
        self._minimax_url = "https://api.minimax.chat/v1/t2a_v2"

        # OpenAI TTS config (gpt-4o-mini-tts — ChatGPT 同款技术)
        self.openai_tts_key = str(tts_config.get("openai_tts_key", "") or os.environ.get("OPENAI_API_KEY", ""))
        self.openai_tts_voice = str(tts_config.get("openai_tts_voice", "alloy"))
        self.openai_tts_model = str(tts_config.get("openai_tts_model", "gpt-4o-mini-tts"))
        self.openai_tts_instructions = str(tts_config.get(
            "openai_tts_instructions",
            "你是 Jarvis，说话像一个亲切聪明的朋友。语气自然温暖，有情感，不要像机器人。中文为主。",
        ))

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _tts_cache_key(self, text: str, emotion: str) -> str:
        """Deterministic cache key from text + voice + emotion."""
        raw = f"{text}|{self.minimax_voice}|{emotion}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _is_cached_file(self, filepath: str) -> bool:
        """Check if a file path is inside the TTS cache directory."""
        try:
            return Path(filepath).resolve().is_relative_to(self._tts_cache_dir.resolve())
        except (ValueError, TypeError):
            return False

    def _evict_tts_cache(self) -> None:
        """Remove oldest files when cache exceeds max size."""
        files = sorted(self._tts_cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
        while len(files) > self._tts_cache_max:
            files[0].unlink(missing_ok=True)
            files.pop(0)

    def precache(self, phrases: list[str]) -> None:
        """Pre-synthesize common phrases into cache at startup.

        Args:
            phrases: List of short phrases to pre-warm the TTS cache with.
                Phrases longer than 50 characters are skipped.
        """
        from concurrent.futures import ThreadPoolExecutor

        def _synth_one(text: str) -> None:
            cache_key = self._tts_cache_key(text, "calm")
            cache_path = self._tts_cache_dir / f"{cache_key}.mp3"
            if cache_path.exists():
                self.logger.debug("TTS precache already exists: %r", text)
                return
            try:
                self.synth_to_file(text, emotion="")
                self.logger.info("TTS precached: %r", text)
            except Exception as exc:
                self.logger.warning("TTS precache failed for %r: %s", text, exc)

        to_cache = [t for t in phrases if len(t) <= 50]
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="tts-precache") as pool:
            list(pool.map(_synth_one, to_cache))

    def speak(self, text: str, emotion: str = "") -> None:
        """Speak text aloud with optional emotion.

        Args:
            text: Text to speak.
            emotion: SenseVoice emotion label (e.g. "HAPPY", "SAD"). Only used
                by Azure engine; ignored by Edge TTS and pyttsx3.
        """
        if not text.strip():
            return

        if self.engine_name == "openai_tts":
            if not self._tracker or self._tracker.is_available("tts.openai"):
                try:
                    self._speak_openai_tts(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.openai")
                    return
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.openai", str(exc))
                    self.logger.warning("OpenAI TTS failed: %s, trying fallback", exc)

        if self.engine_name == "minimax":
            if not self._tracker or self._tracker.is_available("tts.minimax"):
                try:
                    self._speak_minimax(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.minimax")
                    return
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.minimax", str(exc))
                    self.logger.warning("MiniMax TTS failed: %s, trying fallback", exc)

        if self.engine_name == "azure":
            if not self._tracker or self._tracker.is_available("tts.azure"):
                try:
                    self._speak_azure(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.azure")
                    return
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.azure", str(exc))
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

    # ------------------------------------------------------------------
    # Synthesis-to-file methods (shared by TTSEngine.speak and TTSPipeline)
    # ------------------------------------------------------------------

    def synth_to_file(self, text: str, emotion: str = "") -> tuple[str, bool] | None:
        """Synthesize text to an audio file and return (path, deletable).

        Returns None if the engine plays directly (pyttsx3).
        deletable=True means caller should delete the file after playback.
        deletable=False means the file is cached and must NOT be deleted.
        """
        if self.engine_name == "openai_tts" and self.openai_tts_key:
            if not self._tracker or self._tracker.is_available("tts.openai"):
                try:
                    path = self._synth_openai(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.openai")
                    return path, True
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.openai", str(exc))
                    self.logger.warning("OpenAI TTS synth failed: %s, trying fallback", exc)
        if self.engine_name == "minimax" and self.minimax_key:
            if not self._tracker or self._tracker.is_available("tts.minimax"):
                try:
                    result = self._synth_minimax(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.minimax")
                    return result
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.minimax", str(exc))
                    self.logger.warning("MiniMax TTS synth failed: %s, trying fallback", exc)
        if self.engine_name == "azure" and self.azure_key:
            if not self._tracker or self._tracker.is_available("tts.azure"):
                try:
                    path = self._synth_azure(text, emotion)
                    if self._tracker:
                        self._tracker.record_success("tts.azure")
                    return path, True
                except Exception as exc:
                    if self._tracker:
                        self._tracker.record_failure("tts.azure", str(exc))
                    self.logger.warning("Azure TTS synth failed: %s, trying fallback", exc)
        if self.engine_name == "pyttsx3":
            self._speak_pyttsx3(text)
            return None
        return self._synth_edge(text), True

    def _synth_openai(self, text: str, emotion: str = "") -> str:
        """Synthesize with OpenAI TTS, return temp file path."""
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "openai package is required. Install with: pip install openai"
            ) from exc

        if not self.openai_tts_key:
            raise RuntimeError("OpenAI API key not configured (OPENAI_API_KEY)")

        base_instructions = self.openai_tts_instructions
        emotion_hint = _EMOTION_TO_OPENAI_INSTRUCTION.get(emotion, "")
        instructions = f"{base_instructions} {emotion_hint}" if emotion_hint else base_instructions

        self.logger.info(
            "OpenAI TTS: voice=%s emotion=%s text=%r",
            self.openai_tts_voice, emotion or "neutral", text[:50],
        )

        if self._openai_client is None:
            self._openai_client = OpenAI(api_key=self.openai_tts_key)

        response = self._openai_client.audio.speech.create(
            model=self.openai_tts_model,
            voice=self.openai_tts_voice,
            input=text,
            instructions=instructions,
            response_format="mp3",
            speed=self.speed,
        )

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(response.content)
            return tmp.name

    def _synth_minimax(self, text: str, emotion: str = "") -> tuple[str, bool]:
        """Synthesize with MiniMax TTS, return (path, deletable).

        Short texts (<=50 chars) are cached to disk; deletable=False.
        Long texts go to a temp file; deletable=True.
        """
        if not self.minimax_key:
            raise RuntimeError("MiniMax API key not configured (MINIMAX_API_KEY)")

        minimax_emotion = _EMOTION_TO_MINIMAX.get(emotion, "calm")

        # Cache path for short responses
        if len(text) <= 50:
            cache_key = self._tts_cache_key(text, minimax_emotion)
            cache_path = self._tts_cache_dir / f"{cache_key}.mp3"
            if cache_path.exists():
                cache_path.touch()  # update mtime for LRU eviction
                self.logger.info("TTS cache hit: %r", text[:50])
                self._last_cache_hit = True
                return str(cache_path), False
            self._last_cache_hit = False
        else:
            self._last_cache_hit = None

        payload = {
            "model": self.minimax_model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": self.minimax_voice,
                "speed": self.speed,
                "vol": 5,
                "pitch": 0,
                "emotion": minimax_emotion,
            },
            "audio_setting": {
                "format": "mp3",
                "sample_rate": 32000,
                "channel": 1,
            },
        }

        self.logger.info(
            "MiniMax TTS: voice=%s emotion=%s text=%r",
            self.minimax_voice, minimax_emotion, text[:50],
        )

        resp = self._http_session.post(
            self._minimax_url,
            headers={
                "Authorization": f"Bearer {self.minimax_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        if "data" not in data or "audio" not in data.get("data", {}):
            error_msg = data.get("base_resp", {}).get("status_msg", str(data))
            raise RuntimeError(f"MiniMax TTS error: {error_msg}")

        audio_bytes = bytes.fromhex(data["data"]["audio"])

        if len(text) <= 50:
            # Write atomically to cache dir
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".mp3.tmp", dir=self._tts_cache_dir)
            try:
                with os.fdopen(tmp_fd, "wb") as f:
                    f.write(audio_bytes)
                os.rename(tmp_name, str(cache_path))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self._evict_tts_cache()
            return str(cache_path), False
        else:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp.write(audio_bytes)
                return tmp.name, True

    def _synth_azure(self, text: str, emotion: str = "") -> str:
        """Synthesize with Azure Neural TTS, return temp file path."""
        try:
            import azure.cognitiveservices.speech as speechsdk
        except ImportError as exc:
            raise RuntimeError(
                "azure-cognitiveservices-speech is required. "
                "Install with: pip install azure-cognitiveservices-speech"
            ) from exc

        if not self.azure_key:
            raise RuntimeError("Azure Speech key not configured (AZURE_SPEECH_KEY)")

        from xml.sax.saxutils import escape
        style = _EMOTION_TO_AZURE_STYLE.get(emotion, "chat")
        rate_pct = round((self.speed - 1.0) * 100)
        rate_attr = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        ssml = (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f'<voice name="{self.azure_voice}">'
            f'<mstts:express-as style="{style}">'
            f'<prosody rate="{rate_attr}">'
            f'{escape(text)}'
            '</prosody>'
            '</mstts:express-as>'
            '</voice></speak>'
        )

        speech_config = speechsdk.SpeechConfig(
            subscription=self.azure_key, region=self.azure_region,
        )
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        audio_config = speechsdk.audio.AudioOutputConfig(filename=tmp_path)
        synthesizer = speechsdk.SpeechSynthesizer(
            speech_config=speech_config, audio_config=audio_config,
        )

        self.logger.info("Azure TTS: voice=%s style=%s text=%r", self.azure_voice, style, text[:50])
        result = synthesizer.speak_ssml_async(ssml).get()

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return tmp_path
        Path(tmp_path).unlink(missing_ok=True)
        cancellation = result.cancellation_details
        raise RuntimeError(f"Azure TTS failed: {cancellation.reason} - {cancellation.error_details}")

    def _synth_edge(self, text: str) -> str:
        """Synthesize with edge-tts, return temp file path."""
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts is required for Edge TTS. Install with: pip install edge-tts"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        async def _save() -> None:
            communicate = edge_tts.Communicate(text, self.edge_voice, rate=self.edge_rate)
            await communicate.save(tmp_path)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(lambda: asyncio.run(_save())).result()
        else:
            asyncio.run(_save())
        return tmp_path

    # ------------------------------------------------------------------
    # speak methods — thin wrappers around synth_to_file + play
    # ------------------------------------------------------------------

    def _speak_openai_tts(self, text: str, emotion: str = "") -> None:
        tmp_path = self._synth_openai(text, emotion)
        try:
            self._play_audio_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _speak_minimax(self, text: str, emotion: str = "") -> None:
        tmp_path, deletable = self._synth_minimax(text, emotion)
        try:
            self._play_audio_file(tmp_path)
        finally:
            if deletable:
                Path(tmp_path).unlink(missing_ok=True)

    def _speak_azure(self, text: str, emotion: str = "") -> None:
        tmp_path = self._synth_azure(text, emotion)
        try:
            self._play_audio_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _speak_edge(self, text: str) -> None:
        tmp_path = self._synth_edge(text)
        try:
            self._play_audio_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

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
        system = self._platform
        cmd: list[str] | None = None
        if system == "Darwin":
            cmd = ["afplay", filepath]
        elif system == "Linux":
            for candidate in (
                ["mpv", "--no-video", filepath],
                ["ffplay", "-nodisp", "-autoexit", filepath],
                ["aplay", filepath],
            ):
                import shutil
                if shutil.which(candidate[0]):
                    cmd = candidate
                    break
            if cmd is None:
                self.logger.warning("No audio player found on Linux.")
                return
        elif system == "Windows":
            # PowerShell — no Popen kill support, keep subprocess.run
            ps_cmd = (
                f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'
            )
            try:
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    check=True, capture_output=True, timeout=30,
                )
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                self.logger.warning("Audio playback failed: %s", exc)
            return
        else:
            self.logger.warning("Unsupported platform for audio playback: %s", system)
            return

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with self._play_lock:
                self._play_proc = proc
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.logger.warning("Audio playback timed out.")
            with self._play_lock:
                if self._play_proc:
                    self._play_proc.kill()
        except Exception as exc:
            self.logger.warning("Audio playback failed: %s", exc)
        finally:
            with self._play_lock:
                self._play_proc = None

    def stop(self) -> None:
        """Kill current audio playback immediately."""
        with self._play_lock:
            proc = self._play_proc
            if proc and proc.poll() is None:
                proc.terminate()
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


# ---------------------------------------------------------------------------
# Dual-thread TTS pipeline
# ---------------------------------------------------------------------------

class SentenceType(Enum):
    """Position marker for sentences in a response."""

    FIRST = "first"
    MIDDLE = "middle"
    LAST = "last"


_SENTINEL = object()  # signals worker threads to stop


class TTSPipeline:
    """Dual-thread pipeline: text→TTS synthesis→audio playback.

    Decouples synthesis from playback so sentence N+1 can be synthesized
    while sentence N is still playing.

    Usage::

        pipeline = TTSPipeline(tts_engine)
        pipeline.start()
        pipeline.submit("第一句话。", SentenceType.FIRST)
        pipeline.submit("第二句话。", SentenceType.MIDDLE)
        pipeline.submit("最后一句。", SentenceType.LAST)
        pipeline.wait_done()
        pipeline.stop()
    """

    def __init__(self, engine: TTSEngine) -> None:
        self._engine = engine
        self._text_queue: Queue = Queue()
        self._audio_queue: Queue = Queue()
        self._tts_thread: threading.Thread | None = None
        self._play_thread: threading.Thread | None = None
        self._aborted = threading.Event()
        self._done = threading.Event()
        self.logger = LOGGER

    def start(self) -> None:
        """Start the TTS and playback worker threads."""
        self._aborted.clear()
        self._done.clear()
        self._tts_thread = threading.Thread(
            target=self._tts_worker, name="tts-synth", daemon=True,
        )
        self._play_thread = threading.Thread(
            target=self._play_worker, name="tts-play", daemon=True,
        )
        self._tts_thread.start()
        self._play_thread.start()

    def submit(self, text: str, sentence_type: SentenceType = SentenceType.MIDDLE,
               emotion: str = "") -> None:
        """Enqueue a sentence for synthesis and playback."""
        if not text.strip():
            return
        self._text_queue.put((text, sentence_type, emotion))

    def finish(self) -> None:
        """Signal that no more sentences will be submitted. Non-blocking."""
        self._text_queue.put(_SENTINEL)

    def wait_done(self, timeout: float = 60) -> None:
        """Block until all queued sentences have been played."""
        self._done.wait(timeout=timeout)

    def abort(self) -> list[str]:
        """Cancel all pending sentences, stop playback, return unplayed text.

        Returns:
            List of sentence texts that were queued but not yet played.
        """
        self._aborted.set()
        # Collect remaining text from text_queue
        remaining: list[str] = []
        while not self._text_queue.empty():
            try:
                item = self._text_queue.get_nowait()
                if item is not _SENTINEL and isinstance(item, tuple):
                    remaining.append(item[0])  # (text, sentence_type, emotion)
            except Empty:
                break
        # Drain audio queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Empty:
                break
        # Kill currently playing audio
        self._engine.stop()
        # Unblock workers
        self._text_queue.put(_SENTINEL)
        self._audio_queue.put(_SENTINEL)
        return remaining

    def stop(self) -> None:
        """Stop worker threads (call after wait_done or abort)."""
        # Ensure workers can exit
        self._text_queue.put(_SENTINEL)
        self._audio_queue.put(_SENTINEL)
        if self._tts_thread and self._tts_thread.is_alive():
            self._tts_thread.join(timeout=5)
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=5)

    def _tts_worker(self) -> None:
        """Consume text_queue, synthesize to temp files, push to audio_queue."""
        while not self._aborted.is_set():
            try:
                item = self._text_queue.get(timeout=1)
            except Empty:
                continue

            if item is _SENTINEL:
                self._audio_queue.put(_SENTINEL)
                return

            text, sentence_type, emotion = item
            try:
                result = self._synthesize_to_file(text, emotion)
                if result and not self._aborted.is_set():
                    filepath, deletable = result
                    self._audio_queue.put((filepath, sentence_type, deletable))
            except Exception as exc:
                self.logger.warning("TTS synthesis failed: %s", exc)

    def _play_worker(self) -> None:
        """Consume audio_queue and play files sequentially."""
        while not self._aborted.is_set():
            try:
                item = self._audio_queue.get(timeout=1)
            except Empty:
                continue

            if item is _SENTINEL:
                self._done.set()
                return

            filepath, sentence_type, deletable = item
            try:
                if not self._aborted.is_set():
                    self._engine._play_audio_file(filepath)
            except Exception as exc:
                self.logger.warning("Audio playback failed: %s", exc)
            finally:
                if deletable:
                    Path(filepath).unlink(missing_ok=True)

    def _synthesize_to_file(self, text: str, emotion: str = "") -> tuple[str, bool] | None:
        """Delegate to TTSEngine.synth_to_file — single source of truth."""
        return self._engine.synth_to_file(text, emotion)
