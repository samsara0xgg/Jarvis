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

An answer no actor will play still ends (ADR 0222). The actor is per connection, so a phone with
no voice connection, or one that left, has none: :func:`drop_unplayed` writes the
``surface.speech_dropped`` the actor's ledger would have written, from the log-wide watcher
(:func:`unplayed_answers`) for an answer that ends while its device has no actor, and from the
voice's close for the answers its actor held and never ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import (
    PHONE_VOICE_CHANNEL,
    emit_event,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.surface.phone_player import PhoneAudioStreamPlayer

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.surface.phone_link import PhoneRows, PhoneVoice
    from jarvis.surface.voice_media import StreamingTTSPipeline

LOGGER = logging.getLogger(__name__)

_FEED_POLL_S: Final = 0.05
_OVERLOADED_RETRY_S: Final = 0.05

_ENDS: Final = (
    "surface.playback_completed", "surface.playback_interrupted", "surface.playback_failed",
    "surface.speech_dropped",
)
"""What ends an answer's playback: a lease's terminal, or its never having one (ADR 0106)."""
_ENDED_SQL: Final = (
    "SELECT 1 FROM events WHERE type IN (?, ?, ?, ?) "
    "AND json_extract(payload_json, '$.response_id') = ? LIMIT 1"
)
_OPENS_AFTER_SQL: Final = (
    "SELECT event_uid, payload_json FROM events "
    "WHERE id > ? AND type = 'surface.response_open' ORDER BY id"
)
_OPENS_REMEMBERED: Final = 256
UNPLAYED_NO_ACTOR: Final = "no_phone_voice"
UNPLAYED_LEFT: Final = "phone_disconnected"

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


def drop_unplayed(
    conn: sqlite3.Connection, *, response_id: str, turn_id: str, reason: str,
    source_event_id: str | None,
) -> bool:
    """Write the end of an answer no actor will play, unless it already has one.

    It is ``surface.speech_dropped``, the row the actor's ledger writes for an answer it lets go
    before any of it played (ADR 0106): a lease's ``surface.playback_*`` terminal needs a session
    and a generation an unplayed answer never had. Without a row the next turn reads the answer as
    heard whole. Returns whether a row was written.
    """
    if conn.execute(_ENDED_SQL, (*_ENDS, response_id)).fetchone() is not None:
        return False
    emit_event(
        conn,
        type="surface.speech_dropped",
        payload={"response_id": response_id, "turn_id": turn_id, "reason": reason},
        source_event_id=source_event_id,
        correlation={"turn_id": turn_id},
    )
    return True


def end_open_answers(conn: sqlite3.Connection, device: str, *, after_id: int, reason: str) -> int:
    """End every answer of ``device``'s voice turns opened after row ``after_id`` and not ended.

    The voice's close runs this once its actor has stopped: the actor ends what it played and
    its active answer, and says nothing of the answers queued or still buffering behind it.
    Returns how many rows it wrote.
    """
    written = 0
    mine: dict[str, bool] = {}
    for event_uid, raw in conn.execute(_OPENS_AFTER_SQL, (after_id,)).fetchall():
        payload = json.loads(raw)
        response_id, turn_id = payload.get("response_id"), str(payload.get("turn_id", ""))
        if not isinstance(response_id, str):
            continue
        if turn_id not in mine:
            mine[turn_id] = turn_origin(conn, turn_id) == (PHONE_VOICE_CHANNEL, device)
        if mine[turn_id] and drop_unplayed(
            conn, response_id=response_id, turn_id=turn_id, reason=reason,
            source_event_id=event_uid,
        ):
            written += 1
    return written


class UnplayedAnswers:
    """Ends a phone voice answer that finishes while its device has no actor (ADR 0222).

    Fed every response row of the log in order. It remembers the opens of phone voice turns, and
    at the row that ends an answer (emitted, cancelled or failed) asks whether an actor holds it:
    one built before the answer opened is the answer's, and its ledger or its voice's close
    writes the end. With none, the host writes it here: the phone left, never asked for voice,
    or the host cannot speak.
    """

    def __init__(self, owner_boot: Callable[[str], int | None]) -> None:
        """``owner_boot(device)``: the log row the device's actor was built at, if it has one."""
        self._owner_boot = owner_boot
        self._opens: dict[str, tuple[str, str, int]] = {}

    def observe(self, conn: sqlite3.Connection, row_id: int, event: Event) -> None:
        """Take one response row; may write the end of the answer it closes."""
        response_id = event.payload.get("response_id")
        if not isinstance(response_id, str):
            return
        turn_id = str(event.payload.get("turn_id", ""))
        if event.type == "surface.response_open":
            channel, device = turn_origin(conn, turn_id)
            if channel == PHONE_VOICE_CHANNEL and device is not None:
                if len(self._opens) >= _OPENS_REMEMBERED:
                    self._opens.pop(next(iter(self._opens)))
                self._opens[response_id] = (device, turn_id, row_id)
            return
        if event.type == "surface.response_chunk":
            return
        held = self._opens.pop(response_id, None)
        if held is None:
            return
        device, turn_id, opened_at = held
        boot = self._owner_boot(device)
        if boot is not None and boot < opened_at:
            return
        reason = (
            UNPLAYED_NO_ACTOR if event.type == "surface.response_emitted"
            else event.type.replace(".", "_")
        )
        drop_unplayed(
            conn, response_id=response_id, turn_id=turn_id, reason=reason,
            source_event_id=event.event_uid,
        )


class PhoneSpeech:
    """Builds one phone connection's voice; the runtime hands :meth:`open` to the route."""

    def __init__(
        self, *, build: BuildPipeline, rows: PhoneRows, ring_seconds: float, event_log: Path,
        estimated_output_latency_s: float = 0.12,
    ) -> None:
        """Keep what every connection's actor is built from; ``event_log`` is where ends go."""
        self._build = build
        self._rows = rows
        self._ring_seconds = ring_seconds
        self._event_log = event_log
        self._latency_s = estimated_output_latency_s
        self._boots: dict[str, int] = {}

    def owner_boot(self, device: str) -> int | None:
        """The log row ``device``'s actor was built at, from before it exists until it is closed."""
        return self._boots.get(device)

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
        self._boots[device] = boot  # the actor owns its answers from here, built or not yet
        try:
            pipeline = await asyncio.to_thread(self._build, player, speaks_phone_turn(device), boot)
        except BaseException:
            self._release(device, boot)
            raise
        if pipeline is None:
            self._release(device, boot)
            return None
        return _Voice(device, player, pipeline, self._rows, boot, self._event_log, self._release)

    def _release(self, device: str, boot: int) -> None:
        if self._boots.get(device) == boot:
            del self._boots[device]


class _Voice:
    """One connection's player, its media actor and the feed between the log and the actor."""

    def __init__(  # noqa: PLR0913 - what one connection's actor is made of
        self, device: str, player: PhoneAudioStreamPlayer, pipeline: StreamingTTSPipeline,
        rows: PhoneRows, boot_high_water_id: int, event_log: Path,
        release: Callable[[str, int], None],
    ) -> None:
        self._device = device
        self._player = player
        self._pipeline = pipeline
        self._rows = rows
        self._boot = boot_high_water_id
        self._event_log = event_log
        self._release = release
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
        """Stop feeding the actor, close it (the player's socket is already gone), end the rest.

        What the actor held and never ended (answers queued or still buffering behind the one it
        played, and answers it was never fed) is ended here (ADR 0222). The actor stops owning its
        device's answers first: one that ends from now on is the log-wide watcher's.
        """
        feed, self._feed = self._feed, None
        if feed is not None:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        try:
            await asyncio.to_thread(self._pipeline.close)
        finally:
            self._release(self._device, self._boot)
            await asyncio.to_thread(self._end_unplayed)

    def _end_unplayed(self) -> None:
        """Best effort: closing a voice never fails on the record of what it did not say."""
        try:
            with contextlib.closing(open_runtime_event_log(self._event_log)) as conn:
                ended = end_open_answers(
                    conn, self._device, after_id=self._boot, reason=UNPLAYED_LEFT,
                )
        except Exception:
            LOGGER.exception("phone %s: ending its unplayed answers failed", self._device)
        else:
            if ended:
                LOGGER.info("phone %s: ended %d unplayed answer(s)", self._device, ended)

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
