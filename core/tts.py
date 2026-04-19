"""Text-to-speech engine with Azure Neural TTS, Edge TTS, and pyttsx3 fallback.

Also provides TTSPipeline — a dual-thread pipeline that decouples TTS synthesis
from audio playback, eliminating inter-sentence pauses during streaming output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import signal
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from dataclasses import dataclass

from core import tts_preprocessor

# Optional — persistent-stream player stack. If any of these imports
# fail, ``_ensure_stream_player`` returns None and we fall back to the
# legacy subprocess path. This keeps the engine usable in environments
# where numpy/miniaudio/soxr aren't installed (minimal test envs etc.).
try:
    import miniaudio
    import numpy as np
    import soxr

    from core.audio_stream_player import AudioStreamPlayer as _StreamPlayerCls

    _STREAM_PLAYER_IMPORTS_OK = True
except ImportError as _exc:  # pragma: no cover — probed at runtime
    _STREAM_PLAYER_IMPORTS_OK = False
    _STREAM_PLAYER_IMPORT_ERROR = str(_exc)

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

# Emotions that should NOT be sent to MiniMax (skipping saves ~500ms server
# inference tax). NEUTRAL/EMO_UNKNOWN/""/None all mean "no special emotion".
_MINIMAX_EMOTION_SKIP = {"NEUTRAL", "EMO_UNKNOWN", "", None}


def _minimax_emotion_effective(emotion: str | None) -> str | None:
    """Map jarvis emotion label → MiniMax emotion value, or None to skip field.

    Returns None for NEUTRAL / EMO_UNKNOWN / "" / None — no emotion field sent.
    Returns the mapped value (e.g. "happy") for active emotions.
    """
    if emotion in _MINIMAX_EMOTION_SKIP:
        return None
    return _EMOTION_TO_MINIMAX.get(emotion, "calm")


# Set of engine names that implement TTSEngine.stream_to_player.
SUPPORTS_STREAMING: set[str] = {"minimax"}


# ---------------------------------------------------------------------------
# MiniMax WebSocket — collect-all path used by _synth_minimax (commit 3).
# Full streaming with player push lives in core/tts_minimax_ws.py (commit 4).
# ---------------------------------------------------------------------------

async def _ws_collect_audio(
    base_url: str,
    api_key: str,
    task_start_payload: dict,
    text: str,
    logger: logging.Logger,
) -> bytes:
    """Open a WS, send task_start + task_continue(text), collect all PCM
    chunks until is_final, return concatenated bytes. Timeouts: 3s connect,
    3s first-chunk, 5s between-chunks. Raises RuntimeError on any failure.
    """
    import websockets  # local import — keeps top-level minimal

    # wss://host/ws/v1/t2a_v2 — convert https://host → wss://host
    ws_url = (
        base_url.replace("https://", "wss://").replace("http://", "ws://")
        + "/ws/v1/t2a_v2"
    )
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        conn = await asyncio.wait_for(
            websockets.connect(ws_url, additional_headers=headers),
            timeout=3.0,
        )
    except Exception as exc:
        raise RuntimeError(f"MiniMax WS connect failed: {exc}") from exc

    try:
        hello = await asyncio.wait_for(conn.recv(), timeout=3.0)
        hello_obj = json.loads(hello)
        if hello_obj.get("base_resp", {}).get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax WS hello rejected: {hello_obj}")

        await conn.send(json.dumps(task_start_payload))
        ts_resp = await asyncio.wait_for(conn.recv(), timeout=3.0)
        ts_obj = json.loads(ts_resp)
        status = ts_obj.get("base_resp", {}).get("status_code", 0)
        if status != 0:
            raise RuntimeError(
                f"MiniMax task_start rejected: {ts_obj.get('base_resp')}"
            )

        await conn.send(json.dumps({"event": "task_continue", "text": text}))

        chunks: list[bytes] = []
        first = True
        while True:
            timeout = 3.0 if first else 5.0
            msg = await asyncio.wait_for(conn.recv(), timeout=timeout)
            obj = json.loads(msg)
            audio_hex = obj.get("data", {}).get("audio", "") or ""
            if audio_hex:
                if len(audio_hex) % 2:
                    audio_hex = audio_hex[:-1]
                chunks.append(bytes.fromhex(audio_hex))
                first = False
            if obj.get("is_final"):
                break

        try:
            await conn.send(json.dumps({"event": "task_finish"}))
        except Exception:
            pass

        return b"".join(chunks)
    finally:
        try:
            await conn.close()
        except Exception:
            pass


@dataclass
class PlaybackResult:
    """Outcome of a streaming TTS playback.

    Attributes:
        completed: True iff is_final received AND player drain finished before abort.
        played_samples: Player's played-samples counter at exit (for WP5 fraction calc).
        total_samples: Total samples written to player this sentence (None if error mid-stream).
        sentence_start_samples: player.played_samples at the moment feed() began.
        subtitle_url: WS-provided subtitle URL (if any; L1 WP5 input).
        raised: Exception surfaced by stream_to_player; None on success path.
    """
    completed: bool = False
    played_samples: int = 0
    total_samples: int | None = None
    sentence_start_samples: int = 0
    subtitle_url: str | None = None
    raised: Exception | None = None


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
        # WP7 soft-stop state: True while _play_proc is paused via SIGSTOP.
        # Tracked separately from process liveness so we can no-op idempotent
        # resume calls.
        self._paused = False

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
        self.minimax_model = str(tts_config.get("minimax_model", "speech-2.8-turbo"))
        self.minimax_voice = str(tts_config.get("minimax_voice", "male-qn-qingse"))
        # Volume default 1 (int). MiniMax API expects int 0-10; floats may 422
        # against strict OpenAPI integer validators. Clamp to [1, 10].
        raw_vol = tts_config.get("minimax_volume", 1)
        try:
            _vol = int(round(float(raw_vol)))
        except (TypeError, ValueError):
            _vol = 1
        self.minimax_volume = max(1, min(10, _vol))
        # Base URL is region-scoped: `.chat` domestic, `.io` / `-uw.io` international.
        # Path `/v1/t2a_v2` is the same across regions; WS variant reuses this base.
        self._minimax_base_url = str(
            tts_config.get("minimax_base_url", "https://api-uw.minimax.io")
        ).rstrip("/")
        self._minimax_url = f"{self._minimax_base_url}/v1/t2a_v2"
        self._minimax_ws_enabled = bool(tts_config.get("minimax_ws", True))
        self._minimax_prewarm_enabled = bool(tts_config.get("minimax_prewarm", True))

        # TTS text preprocessor config (strips emoji/brackets/asterisks/etc.).
        # All filters default-on; can be disabled per-key in config.yaml.
        self._preprocessor_config = tts_config.get("tts_preprocessor", {})

        # Persistent-stream player (replaces subprocess afplay/mpv/ffplay for
        # soft-stop + zero inter-sentence gap). Lazy-initialized on first
        # playback; falls back to subprocess if import or init fails.
        sp_cfg = tts_config.get("stream_player", {}) if tts_config else {}
        self._stream_player_enabled = bool(sp_cfg.get("enabled", True))
        self._stream_player_sample_rate = int(sp_cfg.get("sample_rate", 48000))
        self._stream_player_ring_seconds = float(sp_cfg.get("ring_seconds", 2.0))
        self._duck_volume = float(sp_cfg.get("duck_volume", 0.3))
        self._duck_ramp_ms = float(sp_cfg.get("duck_ramp_ms", 30.0))
        self._unduck_ramp_ms = float(sp_cfg.get("unduck_ramp_ms", 10.0))
        self._stream_player: Any = None
        self._stream_player_init_failed = False  # sticky: don't retry init spam

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
        """Deterministic cache key from engine + text + voice + emotion.

        Engine name is part of the key: different engines produce audibly
        different output for the same text, so sharing cache entries across
        engines would read the wrong voice after a switch.

        Emotion is normalized to "" for None-ish inputs (NEUTRAL / EMO_UNKNOWN
        / None / "") — all produce identical audio under the emotion-skip
        rule, so they must share a cache entry.
        """
        emo_norm = emotion if emotion and emotion not in ("NEUTRAL", "EMO_UNKNOWN") else ""
        raw = f"{self.engine_name}|{text}|{self.minimax_voice}|{emo_norm}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _is_cached_file(self, filepath: str) -> bool:
        """Check if a file path is inside the TTS cache directory."""
        try:
            return Path(filepath).resolve().is_relative_to(self._tts_cache_dir.resolve())
        except (ValueError, TypeError):
            return False

    def _evict_tts_cache(self) -> None:
        """Remove oldest files when cache exceeds max size.

        Globs both .mp3 (legacy) and .pcm (ws streaming) — cache spans both
        formats during migration; LRU applies equally.
        """
        files = sorted(
            list(self._tts_cache_dir.glob("*.mp3"))
            + list(self._tts_cache_dir.glob("*.pcm")),
            key=lambda p: p.stat().st_mtime,
        )
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
            # MiniMax path now caches as .pcm (commit 3); other engines still .mp3.
            ext = "pcm" if self.engine_name == "minimax" else "mp3"
            cache_path = self._tts_cache_dir / f"{cache_key}.{ext}"
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
        text = tts_preprocessor.clean(text, self._preprocessor_config)
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

        Returns None if the engine plays directly (pyttsx3) or if
        preprocessing leaves nothing to speak.
        deletable=True means caller should delete the file after playback.
        deletable=False means the file is cached and must NOT be deleted.
        """
        text = tts_preprocessor.clean(text, self._preprocessor_config)
        if not text.strip():
            return None
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
        """Synthesize with MiniMax TTS via WebSocket, return (path, deletable).

        Collects all audio chunks (PCM 32kHz mono 16-bit), writes to a
        `.pcm` file. Short texts (<=50 chars) cache to disk with
        deletable=False; long texts go to a temp file with deletable=True.
        """
        if not self.minimax_key:
            raise RuntimeError("MiniMax API key not configured (MINIMAX_API_KEY)")

        minimax_emotion = _minimax_emotion_effective(emotion)  # None means skip field

        # Cache path for short responses (suffix .pcm — new format vs legacy .mp3)
        if len(text) <= 50:
            cache_key = self._tts_cache_key(text, minimax_emotion or "")
            cache_path = self._tts_cache_dir / f"{cache_key}.pcm"
            if cache_path.exists():
                cache_path.touch()
                self.logger.info("TTS cache hit: %r", text[:50])
                self._last_cache_hit = True
                return str(cache_path), False
            self._last_cache_hit = False
        else:
            self._last_cache_hit = None

        voice_setting = {
            "voice_id": self.minimax_voice,
            "speed": self.speed,
            "vol": self.minimax_volume,
            "pitch": 0,
        }
        if minimax_emotion is not None:
            voice_setting["emotion"] = minimax_emotion
        task_start_payload = {
            "event": "task_start",
            "model": self.minimax_model,
            "voice_setting": voice_setting,
            "audio_setting": {
                "format": "pcm",
                "sample_rate": 32000,
                "bitrate": 128000,
                "channel": 1,
            },
        }

        self.logger.info(
            "MiniMax WS collect: voice=%s emotion=%s text=%r",
            self.minimax_voice, minimax_emotion or "(skipped)", text[:50],
        )

        audio_bytes = asyncio.run(
            _ws_collect_audio(
                self._minimax_base_url,
                self.minimax_key,
                task_start_payload,
                text,
                self.logger,
            )
        )

        if len(audio_bytes) == 0:
            raise RuntimeError("MiniMax WS returned empty audio")

        if len(text) <= 50:
            tmp_fd, tmp_name = tempfile.mkstemp(suffix=".pcm.tmp", dir=self._tts_cache_dir)
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

        with tempfile.NamedTemporaryFile(suffix=".pcm", delete=False) as tmp:
            tmp.write(audio_bytes)
            return tmp.name, True

    async def _stream_to_player_async(
        self,
        text: str,
        emotion: str | None,
        player: Any,
        ws_client: Any,
        abort_event: threading.Event,
    ) -> PlaybackResult:
        """Async core of stream_to_player. Session must already be open."""
        result = PlaybackResult(sentence_start_samples=getattr(player, "played_samples", 0))
        total_samples = 0
        aborted = False
        try:
            write_async = getattr(player, "write_async", None)
            use_async = asyncio.iscoroutinefunction(write_async)
            async for pcm_f32 in ws_client.feed(text):
                if abort_event.is_set():
                    aborted = True
                    break
                if use_async:
                    await write_async(pcm_f32)
                else:
                    player.write(pcm_f32)
                total_samples += len(pcm_f32)
            # Players with an async drain pipeline (e.g. BrowserWSPlayer's
            # paced queue) need to finish sending before we declare the
            # sentence played. On abort, fast-cancel instead of draining.
            aclose = getattr(player, "aclose", None)
            if asyncio.iscoroutinefunction(aclose):
                if aborted:
                    abort_fn = getattr(player, "abort", None)
                    if callable(abort_fn):
                        abort_fn()
                await aclose()
            drained = await asyncio.to_thread(player.drain, 5.0)
            result.total_samples = total_samples
            result.played_samples = getattr(player, "played_samples", 0)
            result.subtitle_url = ws_client.last_subtitle_url
            result.completed = bool(drained) and not abort_event.is_set()
        except Exception as exc:
            result.raised = exc
            result.total_samples = total_samples
            result.played_samples = getattr(player, "played_samples", 0)
            result.subtitle_url = getattr(ws_client, "last_subtitle_url", None)
        return result

    def stream_to_player(
        self,
        text: str,
        emotion: str,
        player: Any,
        ws_client: Any,
        abort_event: threading.Event,
    ) -> PlaybackResult:
        """Sync entry point. Runs the async feed+play on an event loop.

        Called from TTSPipeline._tts_worker (a non-asyncio thread). Uses
        asyncio.run to drive the coroutine — simple and safe given one
        sentence at a time per pipeline.
        """
        effective = _minimax_emotion_effective(emotion)
        try:
            return asyncio.run(
                self._stream_to_player_async(
                    text, effective, player, ws_client, abort_event,
                )
            )
        except Exception as exc:
            return PlaybackResult(raised=exc)

    @staticmethod
    def _build_azure_ssml(text: str, voice: str, style: str, rate_attr: str) -> str:
        """Build Azure TTS SSML with full escaping for both text and attributes.

        Attribute values escape `<`, `>`, `&`, and `"` so an unexpected quote
        in voice/style (e.g. from user config) can't break the SSML document.
        """
        from xml.sax.saxutils import escape
        attr_entities = {'"': "&quot;"}
        v = escape(voice, attr_entities)
        s = escape(style, attr_entities)
        r = escape(rate_attr, attr_entities)
        return (
            '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
            f'<voice name="{v}">'
            f'<mstts:express-as style="{s}">'
            f'<prosody rate="{r}">'
            f'{escape(text)}'
            '</prosody>'
            '</mstts:express-as>'
            '</voice></speak>'
        )

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

        style = _EMOTION_TO_AZURE_STYLE.get(emotion, "chat")
        rate_pct = round((self.speed - 1.0) * 100)
        rate_attr = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        ssml = self._build_azure_ssml(text, self.azure_voice, style, rate_attr)

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

    # ------------------------------------------------------------------
    # Stream-player path (miniaudio → soxr → sd.OutputStream)
    # ------------------------------------------------------------------

    def _ensure_stream_player(self) -> Any | None:
        """Lazy-init AudioStreamPlayer. Returns None if disabled/failed.

        Sticky-fails: once init errors once we don't retry (avoids log
        spam on systems without working audio output).
        """
        if not self._stream_player_enabled:
            return None
        if not _STREAM_PLAYER_IMPORTS_OK:
            if not self._stream_player_init_failed:
                self.logger.warning(
                    "stream player deps missing (%s) — falling back to subprocess",
                    _STREAM_PLAYER_IMPORT_ERROR,
                )
                self._stream_player_init_failed = True
            return None
        if self._stream_player_init_failed:
            return None
        if self._stream_player is not None:
            return self._stream_player
        try:
            p = _StreamPlayerCls(
                sample_rate=self._stream_player_sample_rate,
                channels=1,
                ring_seconds=self._stream_player_ring_seconds,
            )
            p.start()
            self._stream_player = p
            self.logger.info(
                "AudioStreamPlayer started at %dHz (ring=%.1fs)",
                self._stream_player_sample_rate, self._stream_player_ring_seconds,
            )
            return p
        except Exception as exc:
            self.logger.warning(
                "AudioStreamPlayer init failed (%s) — falling back to subprocess", exc,
            )
            self._stream_player_init_failed = True
            return None

    def _decode_file_to_pcm(self, filepath: str, target_sr: int) -> "np.ndarray":
        """Decode any supported audio file → mono float32 PCM at target_sr.

        Fast path for `.pcm` files (raw 32kHz mono 16-bit, MiniMax WS format):
        skip miniaudio, read bytes, int16→float32, soxr resample if needed.

        General path: miniaudio's generic decoder (MP3/WAV/FLAC/Vorbis),
        format detection from content. Stereo downmixed to mono. soxr HQ
        resample if source rate ≠ target.
        """
        if filepath.endswith(".pcm"):
            raw = Path(filepath).read_bytes()
            pcm_i16 = np.frombuffer(raw, dtype=np.int16)
            pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
            source_sr = 32000  # MiniMax PCM format fixed at 32kHz
            if source_sr != target_sr:
                pcm_f32 = soxr.resample(pcm_f32, source_sr, target_sr, quality="HQ")
            return pcm_f32.astype(np.float32, copy=False)

        dsf = miniaudio.decode_file(filepath)
        pcm = np.asarray(dsf.samples, dtype=np.int16)
        if dsf.nchannels > 1:
            pcm = pcm.reshape(-1, dsf.nchannels).mean(axis=1).astype(np.int16)
        pcm_f32 = pcm.astype(np.float32) / 32768.0
        if dsf.sample_rate != target_sr:
            pcm_f32 = soxr.resample(pcm_f32, dsf.sample_rate, target_sr, quality="HQ")
        return pcm_f32.astype(np.float32, copy=False)

    def close_stream_player(self) -> None:
        """Close the persistent OutputStream. Safe to call multiple times."""
        if self._stream_player is not None:
            try:
                self._stream_player.stop()
            except Exception as exc:
                self.logger.warning("stream player stop error (ignored): %s", exc)
            self._stream_player = None

    # ------------------------------------------------------------------
    # Playback
    # ------------------------------------------------------------------

    def _play_audio_file(self, filepath: str) -> None:
        """Play an audio file. Uses AudioStreamPlayer if available,
        falls back to subprocess on init failure or decode error."""
        # ---- New path: persistent stream player ----
        player = self._ensure_stream_player()
        if player is not None:
            try:
                pcm = self._decode_file_to_pcm(
                    filepath, self._stream_player_sample_rate,
                )
                player.write(pcm)
                player.drain()
                return
            except Exception as exc:
                # Don't stick-fail the player on a single bad file —
                # could be a malformed MP3 from an API hiccup. Log and
                # retry via subprocess this one time.
                self.logger.warning(
                    "stream player play failed for %s (%s) — subprocess fallback",
                    filepath, exc,
                )

        # ---- Fallback path: subprocess (legacy behavior) ----
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
        """Kill current audio playback immediately.

        For the stream-player path, flush the ring buffer so any pending
        PCM is dropped and the drain() in _play_audio_file returns
        immediately. For the subprocess fallback, terminate afplay.
        Both paths are exercised so abort works regardless of which
        playback mode is active.
        """
        if self._stream_player is not None:
            try:
                self._stream_player.flush()
                # Snap gain back to 1.0 so next sentence isn't stuck ducked
                self._stream_player.set_gain(1.0, ramp_ms=0.0)
            except Exception as exc:
                self.logger.warning("stream player flush error (ignored): %s", exc)
            self._paused = False
        with self._play_lock:
            proc = self._play_proc
            if proc and proc.poll() is None:
                # If suspended, wake it first so terminate can deliver cleanly.
                if self._paused:
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except (ProcessLookupError, PermissionError):
                        pass
                    self._paused = False
                proc.terminate()
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    # ------------------------------------------------------------------
    # WP7 soft-stop: pause/resume the active playback process
    # ------------------------------------------------------------------

    def suspend_playback(self) -> bool:
        """Soft-stop playback: duck volume (preferred) or SIGSTOP subprocess.

        Returns True if a duck or SIGSTOP was actually issued; False if
        nothing was playing or already suspended.

        Preferred path: if the persistent AudioStreamPlayer is running,
        ramp gain down to ``self._duck_volume`` over ``self._duck_ramp_ms``.
        This is sample-accurate, click-free, and resume is a clean ramp
        instead of SIGCONT.

        Fallback: SIGSTOP on the subprocess (Unix only). On macOS afplay,
        the kernel audio driver drains its buffer for ~100-300ms before
        silencing — this produces an audible glitch/loop-tail that the
        stream-player path avoids entirely.
        """
        if self._stream_player is not None and self._stream_player.is_running:
            if self._paused:
                return False
            self._stream_player.duck(self._duck_volume, self._duck_ramp_ms)
            self._paused = True
            return True

        if self._platform not in ("Darwin", "Linux"):
            return False
        with self._play_lock:
            proc = self._play_proc
            if not proc or proc.poll() is not None:
                return False
            if self._paused:
                return False
            try:
                os.kill(proc.pid, signal.SIGSTOP)
                self._paused = True
                return True
            except (ProcessLookupError, PermissionError) as exc:
                self.logger.warning("suspend_playback failed: %s", exc)
                return False

    def resume_playback(self) -> bool:
        """Resume previously soft-stopped playback: unduck or SIGCONT.

        Mirrors ``suspend_playback`` — prefers the stream-player gain
        ramp (clean), falls back to SIGCONT on the subprocess.
        """
        if self._stream_player is not None and self._stream_player.is_running:
            if not self._paused:
                return False
            self._stream_player.unduck(self._unduck_ramp_ms)
            self._paused = False
            return True

        if self._platform not in ("Darwin", "Linux"):
            return False
        with self._play_lock:
            proc = self._play_proc
            if not proc or proc.poll() is not None or not self._paused:
                return False
            try:
                os.kill(proc.pid, signal.SIGCONT)
                self._paused = False
                return True
            except (ProcessLookupError, PermissionError) as exc:
                self.logger.warning("resume_playback failed: %s", exc)
                return False

    def is_paused(self) -> bool:
        """True while the playback process is suspended."""
        return self._paused


# ---------------------------------------------------------------------------
# Dual-thread TTS pipeline
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# WP5 — truncate sentence text to what the user actually heard.
# ---------------------------------------------------------------------------

# Characters to snap truncation forward to (Chinese punctuation + ASCII boundaries)
_WP5_SNAP_CHARS = frozenset("。！？，、；：.!?,;: ")


def _wp5_truncate(
    text: str,
    played_samples: int,
    sentence_start_samples: int,
    total_samples: int | None,
    subtitle_url: str | None,
    sample_rate: int,
    subtitle_fetch_timeout: float = 0.5,
) -> str:
    """Return the prefix of `text` corresponding to what the user heard.

    Three-level degradation:
      L1 — subtitle_url available → fetch ms-precise duration, compute fraction
      L2 — subtitle_url not available but total_samples known → fraction by ring
      L3 — neither signal → empty string (strict: assume unheard)

    After fraction is computed, the character cut is snapped FORWARD to the
    nearest punctuation (within +20% of remaining chars). If no snap target,
    the raw cut is returned. If fraction >= 1.0, returns full text.
    """
    played_this = max(0, played_samples - sentence_start_samples)

    fraction: float | None = None
    # L1: subtitle fetch
    if subtitle_url:
        try:
            import urllib.request
            with urllib.request.urlopen(subtitle_url, timeout=subtitle_fetch_timeout) as r:
                subs = json.loads(r.read().decode("utf-8"))
            if isinstance(subs, list) and subs:
                total_ms = (max(e.get("end", 0) for e in subs)
                            - min(e.get("start", 0) for e in subs))
                if total_ms > 0:
                    played_ms = played_this * 1000 / sample_rate
                    fraction = min(1.0, played_ms / total_ms)
        except Exception:
            pass  # fall through to L2

    # L2: ring buffer fraction
    if fraction is None and total_samples and total_samples > 0:
        fraction = min(1.0, played_this / total_samples)

    # L3: no signal → empty
    if fraction is None or fraction <= 0:
        return ""

    if fraction >= 1.0:
        return text

    k = int(len(text) * fraction)
    if k >= len(text):
        return text
    # Snap forward: look for punctuation in text[k : k + window] where window
    # is 20% of remaining characters (minimum 2 chars, maximum 8).
    window_max = max(2, min(8, int(len(text) * 0.2)))
    for i in range(k, min(len(text), k + window_max)):
        if text[i] in _WP5_SNAP_CHARS:
            return text[:i + 1]  # include the punctuation itself
    return text[:k]


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
        # WP5: track playback progress so callers can reconstruct what the
        # user actually heard before an interrupt arrived.
        self._played_texts: list[str] = []
        self._currently_playing: str | None = None
        self._progress_lock = threading.Lock()
        # Streaming engines need a turn-level WS client. Lazy-created in
        # prewarm() or first submit(). None for non-streaming engines.
        self._ws_client: Any = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._progress_map: dict[str, PlaybackResult] = {}

    def start(self) -> None:
        """Start the TTS and playback worker threads."""
        self._aborted.clear()
        self._done.clear()
        with self._progress_lock:
            self._played_texts = []
            self._currently_playing = None
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

    def prewarm(self, emotion: str = "") -> None:
        """Eagerly open WS session (call on LLM first token).

        No-op if engine doesn't support streaming or ws is disabled.
        Safe to call multiple times — opens only once per turn.
        """
        if self._engine.engine_name not in SUPPORTS_STREAMING:
            return
        if not getattr(self._engine, "_minimax_ws_enabled", False):
            return
        if self._ws_client is not None and self._ws_client.is_open():
            return
        self._open_ws_async(emotion)

    def _open_ws_async(self, emotion: str) -> None:
        """Start a background asyncio loop + open session on it."""
        from core.tts_minimax_ws import MinimaxWSClient

        if self._ws_loop is None:
            self._ws_loop = asyncio.new_event_loop()
            self._ws_thread = threading.Thread(
                target=self._ws_loop.run_forever,
                name="tts-ws-loop",
                daemon=True,
            )
            self._ws_thread.start()

        eff_emotion = _minimax_emotion_effective(emotion)
        eng = self._engine
        player = eng._ensure_stream_player()
        client = MinimaxWSClient(
            base_url=eng._minimax_base_url,
            api_key=eng.minimax_key,
            model=eng.minimax_model,
            voice_id=eng.minimax_voice,
            volume=eng.minimax_volume,
            sample_rate_out=eng._stream_player_sample_rate if player else 48000,
            logger=eng.logger,
        )

        async def _run():
            await client.open_session(eff_emotion)
            client.start_idle_watchdog()

        fut = asyncio.run_coroutine_threadsafe(_run(), self._ws_loop)
        try:
            fut.result(timeout=5.0)
            self._ws_client = client
        except Exception as exc:
            eng.logger.warning("prewarm ws open failed: %s", exc)
            self._ws_client = None

    def _stream_one(self, text: str, sentence_type: Any, emotion: str) -> None:
        """Stream one sentence through the open WS client to the player.

        Lazy-opens the ws if prewarm didn't fire. Records playback result
        (for WP5 truncation in abort()).
        """
        if self._ws_client is None or not self._ws_client.is_open():
            self._open_ws_async(emotion)
            if self._ws_client is None or not self._ws_client.is_open():
                self.logger.warning("WS unavailable, falling back to file path for: %r", text[:30])
                try:
                    result = self._synthesize_to_file(text, emotion)
                    if result and not self._aborted.is_set():
                        filepath, deletable = result
                        self._audio_queue.put((filepath, sentence_type, deletable, text))
                except Exception as exc:
                    self.logger.warning("Fallback synth failed: %s", exc)
                return

        eng = self._engine
        player = eng._ensure_stream_player()
        if player is None:
            self.logger.warning("Stream player unavailable for: %r", text[:30])
            return

        with self._progress_lock:
            self._currently_playing = text

        async def _drive():
            return await eng._stream_to_player_async(
                text, _minimax_emotion_effective(emotion),
                player, self._ws_client, self._aborted,
            )

        fut = asyncio.run_coroutine_threadsafe(_drive(), self._ws_loop)
        try:
            result: PlaybackResult = fut.result(timeout=60)
        except Exception as exc:
            self.logger.warning("Streaming playback raised: %s", exc)
            result = PlaybackResult(raised=exc)

        with self._progress_lock:
            self._currently_playing = None
            if result.completed:
                self._played_texts.append(text)
            self._progress_map[text] = result

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
            Per "方案 b" in WP5: includes the sentence currently mid-playback
            (it counts as unplayed for memory injection).

        After this call, :attr:`played_texts` exposes the sentences that
        finished playback in full before the abort landed — caller can use
        that to reconstruct what the user heard.
        """
        self._aborted.set()
        with self._progress_lock:
            currently_playing = self._currently_playing

        # Close WS first so feed() exits promptly in streaming mode
        if self._ws_client is not None and self._ws_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ws_client.close_session(), self._ws_loop,
                )
                fut.result(timeout=1)
            except Exception as exc:
                self.logger.warning("ws close on abort: %s", exc)

        # Drain text_queue
        text_remaining: list[str] = []
        while not self._text_queue.empty():
            try:
                item = self._text_queue.get_nowait()
                if item is not _SENTINEL and isinstance(item, tuple):
                    text_remaining.append(item[0])  # (text, ...)
            except Empty:
                break
        # Drain audio_queue (text is the 4th element, see _tts_worker)
        audio_remaining: list[str] = []
        while not self._audio_queue.empty():
            try:
                item = self._audio_queue.get_nowait()
                if item is not _SENTINEL and isinstance(item, tuple) and len(item) >= 4:
                    audio_remaining.append(item[3])
            except Empty:
                break
        # Kill currently playing audio (flush ring buffer + kill subprocess)
        self._engine.stop()
        # Unblock workers
        self._text_queue.put(_SENTINEL)
        self._audio_queue.put(_SENTINEL)

        # WP5: currently-playing sentence — if we have streaming progress,
        # truncate text to "what user heard" + put remaining in unplayed.
        # For legacy (non-streaming) engines, currently_playing is whole-unheard.
        current_unplayed = ""
        if currently_playing:
            eng = self._engine
            pr: PlaybackResult | None = self._progress_map.get(currently_playing)
            player = getattr(eng, "_stream_player", None)
            if pr is not None and player is not None:
                sr_out = getattr(eng, "_stream_player_sample_rate", 48000)
                heard = _wp5_truncate(
                    text=currently_playing,
                    played_samples=getattr(player, "played_samples", 0),
                    sentence_start_samples=pr.sentence_start_samples,
                    total_samples=pr.total_samples,
                    subtitle_url=pr.subtitle_url,
                    sample_rate=sr_out,
                )
                if heard:
                    with self._progress_lock:
                        self._played_texts.append(heard)
                    tail = currently_playing[len(heard):].lstrip()
                    current_unplayed = tail
                else:
                    current_unplayed = currently_playing  # L3: unheard
            else:
                current_unplayed = currently_playing  # legacy / no progress

        unplayed: list[str] = []
        if current_unplayed:
            unplayed.append(current_unplayed)
        unplayed.extend(audio_remaining)
        unplayed.extend(text_remaining)
        return unplayed

    @property
    def played_texts(self) -> list[str]:
        """Sentences that finished playback in full before abort()/wait_done()."""
        with self._progress_lock:
            return list(self._played_texts)

    def stop(self) -> None:
        """Stop worker threads + ws loop (call after wait_done or abort)."""
        self._text_queue.put(_SENTINEL)
        self._audio_queue.put(_SENTINEL)
        if self._tts_thread and self._tts_thread.is_alive():
            self._tts_thread.join(timeout=5)
        if self._play_thread and self._play_thread.is_alive():
            self._play_thread.join(timeout=5)
        # WS client close (turn-end)
        if self._ws_client is not None and self._ws_loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ws_client.close_session(), self._ws_loop,
                )
                fut.result(timeout=2)
            except Exception as exc:
                self.logger.warning("ws close_session on stop: %s", exc)
            self._ws_client = None
        if self._ws_loop is not None:
            self._ws_loop.call_soon_threadsafe(self._ws_loop.stop)
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=2)
            self._ws_loop = None
            self._ws_thread = None

    def _tts_worker(self) -> None:
        """Consume text_queue; synthesize or stream per engine capability."""
        while not self._aborted.is_set():
            try:
                item = self._text_queue.get(timeout=1)
            except Empty:
                continue

            if item is _SENTINEL:
                # Always propagate so _play_worker exits cleanly in both
                # streaming and legacy modes.
                self._audio_queue.put(_SENTINEL)
                return

            text, sentence_type, emotion = item

            # Streaming route — minimax with ws enabled
            if (self._engine.engine_name in SUPPORTS_STREAMING
                    and getattr(self._engine, "_minimax_ws_enabled", False)):
                try:
                    self._stream_one(text, sentence_type, emotion)
                except Exception as exc:
                    self.logger.warning("streaming route failed: %s", exc)
                continue

            # Legacy file-based route
            try:
                result = self._synthesize_to_file(text, emotion)
                if result and not self._aborted.is_set():
                    filepath, deletable = result
                    # WP5: carry text alongside the audio so abort() can list
                    # the unplayed text from this queue too.
                    self._audio_queue.put((filepath, sentence_type, deletable, text))
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

            # WP5: audio_queue items carry text (4th elem) so we can
            # track currently_playing + played_texts.
            filepath, sentence_type, deletable, text = item
            with self._progress_lock:
                self._currently_playing = text
            played_to_completion = False
            try:
                if not self._aborted.is_set():
                    self._engine._play_audio_file(filepath)
                    played_to_completion = not self._aborted.is_set()
            except Exception as exc:
                self.logger.warning("Audio playback failed: %s", exc)
            finally:
                with self._progress_lock:
                    self._currently_playing = None
                    if played_to_completion:
                        self._played_texts.append(text)
                if deletable:
                    Path(filepath).unlink(missing_ok=True)

    def _synthesize_to_file(self, text: str, emotion: str = "") -> tuple[str, bool] | None:
        """Delegate to TTSEngine.synth_to_file — single source of truth."""
        return self._engine.synth_to_file(text, emotion)
