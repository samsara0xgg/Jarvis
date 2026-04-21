"""MiniMax T2A WebSocket client with turn-level session reuse.

Protocol (from https://platform.minimax.io/docs/guides/speech-t2a-websocket):

    connect → connected_success
    ↓
    task_start(voice/audio settings) → task_started
    ↓ (can be repeated within one session)
    task_continue(text) → audio chunks (hex pcm) → is_final
    ↓
    task_finish → ws.close

One client wraps one WS connection. `open_session` is called eagerly on LLM
first token (prewarm); `feed(text)` is called per sentence; `close_session`
fires when the turn finishes or 2s of idle elapses.

Used by `TTSEngine.stream_to_player` (commit 5); not a standalone API.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import numpy as np
import soxr

LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class WSConnectError(RuntimeError):
    """WebSocket connect or handshake failed."""


class WSProtocolError(RuntimeError):
    """Server returned a non-zero status_code in base_resp."""


class WSChunkTimeout(RuntimeError):
    """Server did not send a chunk / is_final within the expected window."""


# ---------------------------------------------------------------------------
# MinimaxWSClient
# ---------------------------------------------------------------------------

class MinimaxWSClient:
    """Turn-level MiniMax TTS WebSocket client.

    Args:
        base_url: https://api-uw.minimax.io etc.
        api_key: International platform `sk-api-...` key.
        model: e.g. "speech-2.8-turbo"
        voice_id: e.g. "Chinese (Mandarin)_ExplorativeGirl"
        volume: int 1-10.
        sample_rate_out: target rate for resampled PCM (matches AudioStreamPlayer).
        sample_rate_in: MiniMax PCM source rate (32 kHz fixed).
        logger: injected for test capture.
    """

    _CONNECT_TIMEOUT = 3.0
    _TASK_START_TIMEOUT = 3.0
    _FIRST_CHUNK_TIMEOUT = 8.0
    _BETWEEN_CHUNK_TIMEOUT = 5.0
    _IDLE_CLOSE_SECONDS = 2.0

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        voice_id: str,
        volume: int,
        sample_rate_out: int = 48000,
        sample_rate_in: int = 32000,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ws_url = (
            self._base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/ws/v1/t2a_v2"
        )
        self._api_key = api_key
        self._model = model
        self._voice_id = voice_id
        self._volume = volume
        self._sr_in = sample_rate_in
        self._sr_out = sample_rate_out
        self._logger = logger

        self._conn: Any = None
        self._resampler: Any = None  # soxr.ResampleStream, created in open_session
        self._carry: bytes = b""  # odd-byte chunk remainder
        self._last_subtitle_url: str | None = None
        self._session_id: str | None = None
        self._trace_id: str | None = None
        self._last_activity: float = 0.0
        self._idle_task: asyncio.Task | None = None

    @property
    def last_subtitle_url(self) -> str | None:
        return self._last_subtitle_url

    def is_open(self) -> bool:
        return self._conn is not None

    async def open_session(self, emotion: str | None) -> None:
        """Connect + send task_start. `emotion=None` skips the field (saves 500ms)."""
        import websockets

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            self._conn = await asyncio.wait_for(
                websockets.connect(self._ws_url, additional_headers=headers),
                timeout=self._CONNECT_TIMEOUT,
            )
        except Exception as exc:
            self._conn = None
            raise WSConnectError(f"WS connect failed: {exc}") from exc

        try:
            # connected_success
            hello = await asyncio.wait_for(self._conn.recv(), timeout=self._CONNECT_TIMEOUT)
            hello_obj = json.loads(hello)
            self._session_id = hello_obj.get("session_id")
            self._trace_id = hello_obj.get("trace_id")

            # Build task_start
            voice_setting: dict = {
                "voice_id": self._voice_id,
                "speed": 1.0,
                "vol": self._volume,
                "pitch": 0,
            }
            if emotion is not None:
                voice_setting["emotion"] = emotion
            task_start = {
                "event": "task_start",
                "model": self._model,
                "voice_setting": voice_setting,
                "audio_setting": {
                    "format": "pcm",
                    "sample_rate": self._sr_in,
                    "bitrate": 128000,
                    "channel": 1,
                },
                "subtitle_enable": True,  # L1 WP5 precision; server may ignore on WS
            }
            await self._conn.send(json.dumps(task_start))
            ts = await asyncio.wait_for(self._conn.recv(), timeout=self._TASK_START_TIMEOUT)
            ts_obj = json.loads(ts)
            status = ts_obj.get("base_resp", {}).get("status_code", 0)
            if status != 0:
                raise WSProtocolError(
                    f"task_start rejected: {ts_obj.get('base_resp')} "
                    f"(session={self._session_id} trace={self._trace_id})"
                )
        except WSProtocolError:
            # Clean up conn before bubbling
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
            raise

        # Resampler — streaming state across feed() calls
        if self._sr_in != self._sr_out:
            self._resampler = soxr.ResampleStream(
                self._sr_in, self._sr_out, 1, dtype="float32", quality="HQ",
            )
        else:
            self._resampler = None

        self._last_activity = asyncio.get_event_loop().time()
        self._logger.info(
            "MiniMax WS opened (session=%s) emotion=%s model=%s",
            self._session_id, emotion or "(skipped)", self._model,
        )

    async def close_session(self) -> dict[str, Any]:
        """Send task_finish + close. Idempotent. Returns metadata dict."""
        meta = {
            "session_id": self._session_id,
            "trace_id": self._trace_id,
            "subtitle_url": self._last_subtitle_url,
        }
        if self._conn is None:
            return meta
        try:
            await self._conn.send(json.dumps({"event": "task_finish"}))
        except Exception:
            pass
        try:
            await self._conn.close()
        except Exception:
            pass
        self._conn = None
        self._resampler = None
        self._carry = b""
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None
        return meta

    async def feed(self, text: str) -> AsyncIterator[np.ndarray]:
        """Send task_continue(text), yield resampled float32 PCM chunks until is_final.

        Resample state (self._resampler) is maintained across chunks AND across
        multiple feed() calls within the same session, so boundary artifacts
        are eliminated.

        Chunk alignment: hex-decode may give odd bytes (half int16 sample).
        We carry the trailing byte forward into the next chunk before int16
        reshape. Leftover unpaired byte at is_final is dropped (inaudible).

        Raises WSChunkTimeout if server stalls beyond the per-chunk deadlines.
        """
        if self._conn is None:
            raise RuntimeError("feed() called before open_session")

        await self._conn.send(json.dumps({"event": "task_continue", "text": text}))

        first = True
        while True:
            timeout = self._FIRST_CHUNK_TIMEOUT if first else self._BETWEEN_CHUNK_TIMEOUT
            try:
                msg = await asyncio.wait_for(self._conn.recv(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise WSChunkTimeout(
                    f"WS chunk timeout after {timeout}s "
                    f"(session={self._session_id})"
                ) from exc

            obj = json.loads(msg)
            sub = obj.get("subtitle_file") or obj.get("data", {}).get("subtitle_file")
            if sub:
                self._last_subtitle_url = sub

            audio_hex = obj.get("data", {}).get("audio", "") or ""
            if audio_hex:
                if len(audio_hex) % 2:
                    audio_hex = audio_hex[:-1]
                raw = self._carry + bytes.fromhex(audio_hex)
                aligned_len = (len(raw) // 2) * 2
                self._carry = raw[aligned_len:]
                raw = raw[:aligned_len]
                if raw:
                    pcm_i16 = np.frombuffer(raw, dtype=np.int16).copy()
                    pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
                    if self._resampler is not None:
                        pcm_f32 = self._resampler.resample_chunk(pcm_f32)
                    if pcm_f32.size:
                        self._last_activity = asyncio.get_event_loop().time()
                        first = False
                        yield pcm_f32

            if obj.get("is_final"):
                if self._resampler is not None:
                    tail = self._resampler.resample_chunk(
                        np.zeros(0, dtype=np.float32), last=True,
                    )
                    if tail.size:
                        yield tail
                return

    def start_idle_watchdog(self) -> None:
        """Start a background task that closes the session after _IDLE_CLOSE_SECONDS
        without any feed() activity. Called by the prewarm path — if the LLM
        never produces TTS-worthy text, the WS closes on its own."""
        if self._idle_task and not self._idle_task.done():
            return
        loop = asyncio.get_event_loop()
        self._idle_task = loop.create_task(self._idle_watcher())

    async def _idle_watcher(self) -> None:
        while self._conn is not None:
            await asyncio.sleep(0.05)
            now = asyncio.get_event_loop().time()
            if now - self._last_activity > self._IDLE_CLOSE_SECONDS:
                self._logger.info(
                    "MiniMax WS idle > %.1fs, auto-closing (session=%s)",
                    self._IDLE_CLOSE_SECONDS, self._session_id,
                )
                await self.close_session()
                return
