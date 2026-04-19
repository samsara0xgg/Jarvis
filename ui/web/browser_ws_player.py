# ui/web/browser_ws_player.py
"""Browser-side WebSocket TTS player adapter.

Used by `TTSEngine.stream_to_player` when the Live2D browser pet mode has
an active `/api/tts/stream` WebSocket. Implements the subset of the Player
contract that `_stream_to_player_async` touches (`write`, `played_samples`,
`drain`). Single-sentence scoped: instantiate per `on_sentence` call,
throw away after the sentence finishes.

Not thread-safe across sentences; the TTS pipeline serializes sentences.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)

# MiniMax TTS returns PCM ~5-10x faster than real-time. A ~2-minute turn's
# worth of audio can arrive in ~15 seconds of wall time. The client-side
# AudioWorklet ring buffer is sized to absorb this (see
# ui/web/js/core/audio/pcm-player-processor.js) — we do NOT pace server-side
# because write() is called from `_stream_to_player_async` running on the
# FastAPI loop, and a blocking time.sleep would stall the entire event loop
# (WS frames, SSE events, everything).
_SAMPLE_RATE = 32000


class BrowserWSPlayer:
    """Forwards int16LE @ 32 kHz mono PCM chunks to a browser WebSocket.

    Wire format (matches spec D4):
        bytes[0..2)  uint16 LE  sentence_index
        bytes[2..)   int16  LE  PCM samples

    Args:
        ws: A FastAPI/Starlette ``WebSocket`` (or anything with an awaitable
            ``send_bytes(bytes)`` method).
        sentence_index: The 0-based sentence index within this turn. Packed
            into the first 2 bytes of every forwarded frame.
        loop: The asyncio event loop that owns ``ws`` (FastAPI's main loop).
            ``write()`` runs on the TTSEngine's private asyncio thread, so
            we must cross back to ``loop`` via ``run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        ws: Any,
        sentence_index: int,
        loop: asyncio.AbstractEventLoop,
        pace: bool = False,
    ) -> None:
        self._ws = ws
        self._idx = sentence_index
        self._loop = loop
        self._header = struct.pack("<H", sentence_index)
        self.played_samples: int = 0  # monotonic, same API as AudioStreamPlayer
        self._pace = pace  # deprecated — retained for tests; never True in prod
        self._chunks_sent = 0

    def write(self, pcm_f32: np.ndarray) -> None:
        """Called per PCM chunk from ``MinimaxWSClient.feed()``.

        Converts float32 → int16 LE, prepends the 2-byte sentence_index
        header, and schedules ``ws.send_bytes`` on the FastAPI event loop.
        Non-blocking — returns immediately after scheduling.
        """
        if pcm_f32.size == 0:
            return
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        payload = self._header + pcm_i16.tobytes()
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send_bytes(payload), self._loop,
            )
            self._chunks_sent += 1
            LOGGER.info(
                "[ws] idx=%d chunk #%d: %d samples (%.2fs), played_total=%d",
                self._idx, self._chunks_sent, pcm_i16.size,
                pcm_i16.size / _SAMPLE_RATE, self.played_samples + pcm_i16.size,
            )
        except Exception as exc:
            LOGGER.warning(
                "[ws] idx=%d send_bytes scheduling failed: %s",
                self._idx, exc, exc_info=True,
            )
        self.played_samples += pcm_i16.size

    def drain(self, timeout: float = 5.0) -> bool:
        """Always returns True (optimistic drain).

        The playback buffer lives in the browser; there is no server-side
        drain to wait on. Returning True makes
        ``TTSEngine._stream_to_player_async`` set
        ``PlaybackResult.completed=True`` so WP5 records this sentence in
        ``played_texts``. Returning None/False would cause the sentence to
        be treated as unplayed and re-injected on the next turn.

        See design doc D11 for the edge-case tradeoff (WS death at
        sentence_end). `timeout` is ignored; kept for API compatibility
        with ``AudioStreamPlayer.drain``.
        """
        _ = timeout
        return True
