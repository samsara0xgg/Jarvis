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
    ) -> None:
        self._ws = ws
        self._idx = sentence_index
        self._loop = loop
        self._header = struct.pack("<H", sentence_index)
        self.played_samples: int = 0  # monotonic, same API as AudioStreamPlayer

    def write(self, pcm_f32: np.ndarray) -> None:
        """Called per PCM chunk from ``MinimaxWSClient.feed()``.

        Converts float32 → int16 LE, prepends the 2-byte sentence_index
        header, and schedules ``ws.send_bytes`` on the FastAPI event loop.
        """
        if pcm_f32.size == 0:
            return
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        payload = self._header + pcm_i16.tobytes()
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send_bytes(payload), self._loop,
            )
        except Exception as exc:
            # Browser WS may have closed; stream_to_player's abort_event
            # handles the cancel path. We just log and drop this chunk.
            LOGGER.debug("BrowserWSPlayer send_bytes scheduling failed: %s", exc)
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
