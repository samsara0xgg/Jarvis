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
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

# MiniMax TTS returns PCM ~5-10x faster than real-time. Two backpressure
# paths exist:
#
# 1. ``write_async`` (preferred): awaited directly by
#    ``TTSEngine._stream_to_player_async`` on the FastAPI loop. ``await
#    ws.send_bytes`` blocks when the starlette send queue / TCP window is
#    full, pausing the upstream ``async for pcm in ws_client.feed()`` loop.
#    This propagates TCP-layer backpressure to MiniMax for free.
#
# 2. ``write`` (legacy, fire-and-forget): schedules ``ws.send_bytes`` via
#    ``run_coroutine_threadsafe`` and returns immediately. No backpressure.
#    Kept only for callers that have a sync hot path they can't easily
#    ``await`` — in practice the streaming path always goes through
#    ``write_async``.
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

    # Max seconds of audio we allow to be sent ahead of wall-clock. Starlette's
    # ws.send_bytes returns as soon as the payload is enqueued in its internal
    # asyncio queue, so TCP-level backpressure doesn't propagate — MiniMax
    # fills the send queue at ~10× real-time and burns ring+memory. This
    # explicit pacer caps the lead so ring depth stays under ~3s regardless
    # of how fast upstream produces. Browser ring is 5s → 2s headroom.
    _MAX_AHEAD_SECONDS = 3.0

    def __init__(
        self,
        ws: Any,
        sentence_index: int,
        loop: asyncio.AbstractEventLoop,
        pace: bool = False,
        abort_event: Any = None,
        get_cursor: Callable[[], int] | None = None,
    ) -> None:
        self._ws = ws
        self._idx = sentence_index
        self._loop = loop
        self._header = struct.pack("<H", sentence_index)
        # played_samples is turn-cumulative, read from the browser AudioWorklet
        # cursor via get_cursor. Matches AudioStreamPlayer semantics (monotonic
        # across sentences in the turn), which is what WP5 truncation expects.
        # Without get_cursor (tests/legacy), returns 0 — WP5 will fall through
        # to L3 (whole-sentence unheard) rather than report encoded-but-unplayed.
        self._get_cursor = get_cursor
        self._pace = pace  # deprecated — retained for tests; never True in prod
        self._chunks_sent = 0
        self._pace_start: float | None = None
        # Decoupled send pipeline. ``write_async`` puts encoded payloads
        # into ``_send_q`` at MiniMax's full speed; ``_drain`` pops from it
        # at playback rate. Pacing in write_async itself would stall the
        # MiniMax recv loop in ws_client.feed(), causing keepalive timeout.
        self._send_q: asyncio.Queue | None = None
        self._drain_task: asyncio.Task | None = None
        self._drain_samples: int = 0
        self._closed = False
        # Optional per-turn abort_event. Drain loop polls it every
        # iteration + after each paced sleep so a new_chat cancel stops
        # residual paced PCM within ~1s instead of draining the full queue.
        self._abort_event = abort_event

    def _encode_chunk(self, pcm_f32: np.ndarray) -> bytes | None:
        """Shared encode path for write / write_async. Returns None if empty."""
        if pcm_f32.size == 0:
            return None
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        self._chunks_sent += 1
        return self._header + pcm_i16.tobytes()

    @property
    def played_samples(self) -> int:
        """Samples the browser has actually played (turn-cumulative).

        Read from the AudioWorklet cursor via ``get_cursor``. Returns 0 if
        no cursor callable was supplied — callers that need WP5 truncation
        must wire one up, else the in-progress sentence falls through to
        L3 (whole-sentence unheard)."""
        if self._get_cursor is None:
            return 0
        try:
            return int(self._get_cursor())
        except Exception:
            return 0

    async def write_async(self, pcm_f32: np.ndarray) -> None:
        """Preferred PCM path. Non-blocking on the MiniMax side: encodes
        and queues for the drain task. Pacing happens in ``_drain`` so the
        upstream ``async for pcm in ws_client.feed()`` loop can keep
        recv-ing from MiniMax (otherwise MiniMax's keepalive ping times
        out after ~20s and the session drops with code 1011)."""
        payload = self._encode_chunk(pcm_f32)
        if payload is None:
            return
        if self._send_q is None:
            self._send_q = asyncio.Queue()
            self._drain_task = asyncio.create_task(self._drain_loop())
        await self._send_q.put(payload)

    def _should_stop(self) -> bool:
        if self._closed:
            return True
        if self._abort_event is not None:
            try:
                return bool(self._abort_event.is_set())
            except Exception:
                return False
        return False

    async def _drain_loop(self) -> None:
        """Pace payloads from ``_send_q`` to the browser WS, staying at
        most ``_MAX_AHEAD_SECONDS`` of audio ahead of wall-clock."""
        loop = asyncio.get_event_loop()
        try:
            while True:
                if self._should_stop():
                    return
                payload = await self._send_q.get()
                if payload is None:
                    return
                if self._should_stop():
                    return
                samples = (len(payload) - 2) // 2
                self._drain_samples += samples
                if self._pace_start is None:
                    self._pace_start = loop.time()
                audio_sec = self._drain_samples / _SAMPLE_RATE
                wall_sec = loop.time() - self._pace_start
                ahead = audio_sec - wall_sec
                if ahead > self._MAX_AHEAD_SECONDS:
                    # Cap single sleep so cancel via aclose gets serviced quickly.
                    sleep_for = min(ahead - self._MAX_AHEAD_SECONDS, 1.0)
                    await asyncio.sleep(sleep_for)
                if self._should_stop():
                    return
                try:
                    await self._ws.send_bytes(payload)
                except Exception as exc:
                    LOGGER.warning(
                        "[ws] idx=%d send_bytes failed: %s",
                        self._idx, exc, exc_info=True,
                    )
                    return
        except asyncio.CancelledError:
            pass

    async def aclose(self) -> None:
        """Signal drain done and wait for queued PCM to finish sending.
        Called by ``_stream_to_player_async`` via the drain hook after the
        MiniMax feed loop completes (or aborts)."""
        if self._send_q is not None:
            await self._send_q.put(None)
        if self._drain_task is not None:
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass

    def abort(self) -> None:
        """Fast-path cancel: mark closed and cancel drain task. Safe from
        any coroutine on the player's loop."""
        self._closed = True
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()

    def write(self, pcm_f32: np.ndarray) -> None:
        """Legacy fire-and-forget path. Schedules ``ws.send_bytes`` on the
        FastAPI loop and returns immediately. No backpressure — prefer
        ``write_async`` in ``async`` callers."""
        payload = self._encode_chunk(pcm_f32)
        if payload is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send_bytes(payload), self._loop,
            )
        except Exception as exc:
            LOGGER.warning(
                "[ws] idx=%d send_bytes scheduling failed: %s",
                self._idx, exc, exc_info=True,
            )

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
