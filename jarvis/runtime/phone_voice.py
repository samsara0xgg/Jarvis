"""ADR 0209: one more media actor for each phone in a voice conversation.

The route (:mod:`jarvis.surface.phone_link`) is L5 and cannot reach the provider, the log or the
decision layer; this is the runtime's wiring for it. For each voice connection it builds a
:class:`~jarvis.surface.voice_media.StreamingTTSPipeline` around a
:class:`~jarvis.surface.phone_player.PhoneAudioStreamPlayer` (the actor is unchanged except for
its player), and feeds it the response rows of the turns that phone opened by voice. The Mac's
own actor never speaks those turns (``phone_voice`` is a silent channel for it), and the phone's
actor speaks nothing else: ``speaks_turn`` checks the turn's opening row, which the actor's drain
needs, because it reads every response row up to the one it was handed.

The heard prefix, the captions and the ``surface.playback_*`` rows of a phone turn are written
by this actor from its ledger, into the host's event log. It has no broadcaster: the Mac's
captions and speaking face are the Mac's actor's.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import PHONE_VOICE_CHANNEL, turn_origin
from jarvis.surface.phone_player import PhoneAudioStreamPlayer

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.shared import Event
    from jarvis.surface.phone_link import PhoneRows, PhoneVoice
    from jarvis.surface.voice_media import StreamingTTSPipeline

LOGGER = logging.getLogger(__name__)

_FEED_POLL_S: Final = 0.05
_OVERLOADED_RETRY_S: Final = 0.05

type BuildPipeline = Callable[
    [PhoneAudioStreamPlayer, Callable[[sqlite3.Connection, str], bool], int],
    StreamingTTSPipeline | None,
]
"""``(player, speaks_turn, boot_high_water_id) -> pipeline``, or ``None`` if the host cannot
build one. Runs on a worker thread: the actor's constructor starts its own."""


def speaks_phone_turn(device: str) -> Callable[[sqlite3.Connection, str], bool]:
    """The actor's ``speaks_turn`` for ``device``: turns it opened by voice, and only those."""

    def speaks(conn: sqlite3.Connection, turn_id: str) -> bool:
        return turn_origin(conn, turn_id) == (PHONE_VOICE_CHANNEL, device)

    return speaks


class PhoneSpeech:
    """Builds one phone connection's voice; the runtime hands :meth:`open` to the route."""

    def __init__(
        self, *, build: BuildPipeline, rows: PhoneRows, ring_seconds: float,
        estimated_output_latency_s: float = 0.12,
    ) -> None:
        """Keep what every connection's actor is built from."""
        self._build = build
        self._rows = rows
        self._ring_seconds = ring_seconds
        self._latency_s = estimated_output_latency_s

    async def open(
        self, device: str, send_binary: Callable[[bytes], bool],
    ) -> PhoneVoice | None:
        """A voice for ``device``'s connection, or ``None`` when no actor can be built."""
        player = PhoneAudioStreamPlayer(
            send_binary=send_binary,
            ring_seconds=self._ring_seconds,
            generation_safe=True,
            estimated_output_latency_s=self._latency_s,
        )
        boot = self._rows.high_water()
        pipeline = await asyncio.to_thread(self._build, player, speaks_phone_turn(device), boot)
        if pipeline is None:
            return None
        return _Voice(device, player, pipeline, self._rows, boot)


class _Voice:
    """One connection's player, its media actor and the feed between the log and the actor."""

    def __init__(
        self, device: str, player: PhoneAudioStreamPlayer, pipeline: StreamingTTSPipeline,
        rows: PhoneRows, boot_high_water_id: int,
    ) -> None:
        self._device = device
        self._player = player
        self._pipeline = pipeline
        self._rows = rows
        self._boot = boot_high_water_id
        self._feed: asyncio.Task[None] | None = None

    @property
    def player(self) -> PhoneAudioStreamPlayer:
        """The player whose socket this connection is."""
        return self._player

    async def begin(self) -> None:
        """The phone's READY arrived: speak its spoken turns from where the actor was built."""
        if self._feed is None:
            self._feed = asyncio.create_task(self._run_feed(), name=f"phone_voice:{self._device}")

    async def stop_playback(self, reason: str) -> str:
        """The local interrupt: tombstone, DISCARD to the phone, settle on its DISCARD_ACK."""
        return await asyncio.to_thread(
            self._pipeline.stop_foreground_output, None, reason=reason,
        )

    async def close(self) -> None:
        """Stop feeding the actor, then close it (the player's socket is already gone)."""
        feed, self._feed = self._feed, None
        if feed is not None:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        await asyncio.to_thread(self._pipeline.close)

    async def _run_feed(self) -> None:
        """Give the actor each row of this device's spoken turns, in order, as ``_tts_watcher``."""
        cursor = self._rows.cursor(self._device, self._boot, spoken_only=True)
        pending: list[tuple[int, Event]] = []
        while True:
            if not pending:
                try:
                    _after, batch = cursor.poll()
                except Exception:
                    LOGGER.exception("phone %s: reading its spoken rows failed", self._device)
                    batch = []
                pending = list(batch)
            while pending:
                row_id, event = pending[0]
                try:
                    outcome = await self._pipeline.submit_event(
                        row_id=row_id, event=event, origin="watcher",
                    )
                except Exception:
                    LOGGER.exception("phone %s: the media actor refused a row", self._device)
                    outcome = None
                if outcome is not None and outcome.status == "overloaded":
                    await asyncio.sleep(_OVERLOADED_RETRY_S)  # the log is the durable queue
                    break
                pending.pop(0)
            else:
                await asyncio.sleep(_FEED_POLL_S)
