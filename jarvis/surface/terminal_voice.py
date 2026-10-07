"""ADR 0172: the brain's end of a terminal's speech.

A voice terminal runs the media actor (player, ledger, ducking, cues) and needs two things
from the brain: the answer, and a speech provider whose key stays here (ADR 0170). Both ride
the ``/terminal/ws`` link of :mod:`jarvis.surface.terminal_link`; the terminal's end is
:mod:`jarvis.surface.terminal_speaker`. A terminal opts in with ``"voice": true`` in its hello::

    terminal -> brain   {"type": "hello", "tools": [...], "voice": true, "rows_after": 41}
    brain -> terminal   {"type": "ready", "device": "...", "voice": true}

**Rows.** The brain streams the answer's rows to one voice terminal, in log order::

    brain -> terminal   {"type": "row", "id": 57, "event_uid": "...", "event_type":
                         "surface.response_chunk", "schema_version": 1, "ts_epoch_ms": ...,
                         "payload": {...}}

``id`` is the row's id in the brain's log. ``rows_after`` is the last ``id`` the terminal
journaled: a reconnect continues after it. A terminal that has none (its first connection, or
a restart that emptied its journal) starts from the log's high-water mark, so an old answer is
never spoken late.

**Provider sessions.** A terminal's :class:`~jarvis.surface.voice_tts.TTSSession` calls become
requests, and the brain answers each, or streams audio events, under the session's id::

    terminal -> brain   {"type": "tts", "sid": "<32 hex>", "rid": 3, "op": "open",
                         "params": {...}, "response_id": "...", "playback_generation_id": 4}
    brain -> terminal   {"type": "tts", "sid": "...", "rid": 3, "ok": true}
    brain -> terminal   {"type": "tts", "sid": "...", "rid": 3, "ok": false,
                         "error": {"name": "TTSConcurrentSendError", "message": "..."}}
    brain -> terminal   {"type": "tts", "sid": "...", "op": "audio", "n": 0,
                         "event": {"kind": "chunk", "pcm": "<base64>", ...}}
    brain -> terminal   {"type": "tts", "sid": "...", "op": "end"}
    brain -> terminal   {"type": "tts", "sid": "...", "op": "failed", "error": {...}}

``op`` is ``connect``, ``open``, ``send``, ``finish``, ``abort`` or ``close``. ``connect`` and
``open`` carry the session's ``params`` (its ``create_tts_session`` arguments); the first one
creates it. A request with no ``rid`` is not answered (an ``abort`` must not wait). ``audio``
frames are numbered by ``n`` from 0 and arrive in the order the provider produced them; a
chunk is never split or merged, so word boundaries and every event kind survive unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.shared import lang
from jarvis.surface.voice_tts import (
    TTSAudioChunk,
    TTSAudioEvent,
    TTSResponseSegment,
    TTSSegmentFinished,
    TTSSession,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from jarvis.shared import Event
    from jarvis.surface.voice_media import StreamingTTSProvider

LOGGER = logging.getLogger("jarvis.surface.terminal_voice")

RESPONSE_ROW_TYPES: Final = (
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
    "response.cancelled",
    "response.failed",
)
"""What the media actor reads from the log: an answer, and the end of one."""

ROWS_PER_POLL: Final = 200
POLL_INTERVAL_S: Final = 0.05
"""How often the brain looks for new answer rows, as the daemon's own watchers do."""
MAX_SESSIONS: Final = 8
"""Provider sessions one terminal may hold: one playing, a spare, and the cut-off line."""
HEX_ID: Final = re.compile(r"[0-9a-f]{32}")
_MAX_SEGMENT_CHARS: Final = 64 * 1024
_MAX_ERROR_CHARS: Final = 500
_DISCARD_TIMEOUT_S: Final = 2.0
_MAX_IDLE_CLOSE_S: Final = 600.0
_MAX_QUEUE: Final = 256
_MAX_ROUTED_TURNS: Final = 256

type RowBatch = tuple[int, list[tuple[int, Event]]]
"""``(highest id seen, [(id, event) to send])``: ids seen but not sent were not spoken."""


class RowCursor(Protocol):
    """A place in the brain's log from which the next answer rows are read."""

    def poll(self) -> RowBatch:
        """The rows past the cursor that a terminal should speak, advancing the cursor."""


class RowSource(Protocol):
    """Where answer rows come from; the runtime binds it to the event log."""

    def high_water(self) -> int:
        """The latest row's id (0 when the log is empty)."""

    def cursor(self, after: int) -> RowCursor:
        """A cursor that reads rows with ``id > after``."""


def row_frame(row_id: int, event: Event) -> str:
    """One answer row as the ``row`` frame a terminal journals."""
    return json.dumps(
        {
            "type": "row",
            "id": row_id,
            "event_uid": event.event_uid,
            "event_type": event.type,
            "schema_version": event.schema_version,
            "ts_epoch_ms": event.ts_epoch_ms,
            "payload": dict(event.payload),
        },
        ensure_ascii=False,
    )


def encode_event(event: object) -> dict[str, Any]:
    """One provider audio event as JSON; any kind this does not know is an error."""
    if isinstance(event, TTSAudioChunk):
        return {
            "kind": "chunk",
            "sequence": event.sequence,
            "pcm": base64.b64encode(event.pcm).decode("ascii"),
            "sample_rate_hz": event.sample_rate_hz,
            "channels": event.channels,
            "sample_format": event.sample_format,
            "timing_text": event.timing_text,
            "word_boundaries": [[end, ms] for end, ms in event.word_boundaries],
        }
    if isinstance(event, TTSSegmentFinished):
        usage = None if event.usage is None else dict(event.usage)
        return {"kind": "finished", "sequence": event.sequence, "usage": usage}
    msg = f"no wire form for a {type(event).__name__} audio event"
    raise TypeError(msg)


def decode_event(raw: object) -> TTSAudioEvent:
    """The provider audio event a brain's ``audio`` frame carries.

    Raises:
        ValueError: the frame is not one of the event kinds, or a field has the wrong shape.
    """
    if not isinstance(raw, dict):
        msg = "an audio event is a JSON object"
        raise ValueError(msg)  # noqa: TRY004 — one error type for every malformed frame.
    sequence = raw.get("sequence")
    if type(sequence) is not int:
        msg = "an audio event needs an integer sequence"
        raise ValueError(msg)
    if raw.get("kind") == "finished":
        usage = raw.get("usage")
        if usage is not None and not isinstance(usage, dict):
            msg = "usage is an object or null"
            raise ValueError(msg)
        return TTSSegmentFinished(sequence=sequence, usage=usage)
    if raw.get("kind") != "chunk":
        msg = f"unknown audio event kind {raw.get('kind')!r}"
        raise ValueError(msg)
    rate, channels, text = raw.get("sample_rate_hz"), raw.get("channels"), raw.get("timing_text")
    bounds = raw.get("word_boundaries")
    if (
        type(rate) is not int or type(channels) is not int
        or raw.get("sample_format") != "int16_le"
        or not (text is None or isinstance(text, str))
        or not isinstance(bounds, list) or not isinstance(raw.get("pcm"), str)
    ):
        msg = "a chunk has a malformed field"
        raise ValueError(msg)
    try:
        pcm = base64.b64decode(raw["pcm"], validate=True)
        boundaries = tuple((int(end), float(ms)) for end, ms in bounds)
    except (TypeError, ValueError) as exc:
        msg = "a chunk has malformed pcm or word boundaries"
        raise ValueError(msg) from exc
    return TTSAudioChunk(
        sequence=sequence, pcm=pcm, sample_rate_hz=rate, channels=channels,
        timing_text=text, word_boundaries=boundaries,
    )


def error_body(exc: BaseException) -> dict[str, str]:
    """A failure as the wire carries it: the class's name and a bounded message."""
    return {"name": type(exc).__name__, "message": str(exc)[:_MAX_ERROR_CHARS]}


class _Refused(Exception):  # noqa: N818 — a refusal, not a fault.
    """A request the brain will not run: bad shape, no such session, too many."""


def _int(value: object, *, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        msg = f"expected an integer in {low}..{high}"
        raise _Refused(msg)
    return value


@dataclass(eq=False)
class _BrainSession:
    """One terminal's provider session, run here."""

    real: TTSSession
    pump: asyncio.Task[None] | None = None


@dataclass(eq=False)
class Peer:
    """One connected voice terminal, as :class:`BrainVoice` sees it."""

    name: str
    send_text: Callable[[str], Awaitable[None]]
    after: int = 0  # the last answer row this terminal has, in the brain's log
    sessions: dict[str, _BrainSession] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, frame: Mapping[str, Any] | str) -> None:
        """Send one frame whole; frames from different tasks never interleave."""
        text = frame if isinstance(frame, str) else json.dumps(frame, ensure_ascii=False)
        async with self._lock:
            await self.send_text(text)


class BrainVoice:
    """The brain's half of terminal speech: the row stream and the provider sessions.

    ``provider`` is the real speech client that holds the key; every method runs on the loop
    that serves the sockets.
    """

    def __init__(
        self, provider: StreamingTTSProvider, rows: RowSource, *,
        poll_interval_s: float = POLL_INTERVAL_S,
    ) -> None:
        """Serve ``provider``'s sessions, and answer rows from ``rows``, to voice terminals."""
        self._provider = provider
        self._rows = rows
        self._poll_s = poll_interval_s
        self._peers: list[Peer] = []  # oldest connection first
        self._turns: collections.OrderedDict[str, Peer] = collections.OrderedDict()

    def attach(
        self, name: str, send_text: Callable[[str], Awaitable[None]], resume_after: int | None,
    ) -> Peer:
        """Register a terminal that declared voice, before it is told ``ready``.

        Its rows start after ``resume_after``, or at the log's high-water mark when it has
        none or names a row this log never had: taken here, so an answer logged once the
        terminal is ready is never missed, and one logged before is never replayed.
        """
        high = self._rows.high_water()
        peer = Peer(
            name, send_text, after=high if resume_after is None or resume_after > high
            else resume_after,
        )
        self._peers.append(peer)
        return peer

    async def detach(self, peer: Peer) -> None:
        """The link is gone: stop its requests and abort every provider session it held."""
        if peer in self._peers:
            self._peers.remove(peer)
        for task in list(peer.tasks):
            task.cancel()
        if peer.tasks:
            await asyncio.wait(peer.tasks)
        held = list(peer.sessions)
        for sid in held:
            await self._discard(peer, sid)
        LOGGER.info("aborted %d tts session(s) for %s", len(held), peer.name)

    def route(self, turn_id: str, peer: Peer) -> None:
        """``peer`` heard the utterance that opened ``turn_id``: its answer is spoken there."""
        self._turns[turn_id] = peer
        self._turns.move_to_end(turn_id)
        while len(self._turns) > _MAX_ROUTED_TURNS:
            self._turns.popitem(last=False)

    def peer_for_turn(self, turn_id: str) -> Peer | None:
        """The terminal that speaks ``turn_id``.

        The one that heard it while it is still connected, else the voice terminal connected
        last (a turn that began as text).
        """
        peer = self._turns.get(turn_id)
        if peer is not None and peer in self._peers:
            return peer
        return self._peers[-1] if self._peers else None

    def target_for(self, event: Event) -> Peer | None:
        """The terminal that speaks ``event``'s turn (see :meth:`peer_for_turn`)."""
        turn_id = event.payload.get("turn_id")
        return self.peer_for_turn(turn_id if isinstance(turn_id, str) else "")

    # -- rows --------------------------------------------------------------------------

    async def stream_rows(self, peer: Peer) -> None:
        """Send ``peer`` the answer rows this brain's log gets, until cancelled or the link ends."""
        cursor = self._rows.cursor(peer.after)
        while True:
            try:
                _high, batch = cursor.poll()
            except Exception:
                LOGGER.exception("terminal %s: reading answer rows failed; retrying", peer.name)
                await asyncio.sleep(1.0)
                continue
            for row_id, event in batch:
                if self.target_for(event) is peer:
                    await peer.send(row_frame(row_id, event))
            if not batch:
                await asyncio.sleep(self._poll_s)

    # -- provider sessions -------------------------------------------------------------

    def on_frame(self, peer: Peer, frame: Mapping[str, Any]) -> None:
        """Run one ``tts`` request on its own task: an abort must not queue behind a send."""
        task = asyncio.create_task(self._serve(peer, frame))
        peer.tasks.add(task)
        task.add_done_callback(peer.tasks.discard)

    async def _serve(self, peer: Peer, frame: Mapping[str, Any]) -> None:
        sid, rid, op = frame.get("sid"), frame.get("rid"), frame.get("op")
        if not isinstance(sid, str) or HEX_ID.fullmatch(sid) is None or not isinstance(op, str):
            return
        answer: dict[str, Any] = {"type": "tts", "sid": sid, "rid": rid, "ok": True}
        try:
            await self._run(peer, sid, op, frame)
        except asyncio.CancelledError:
            raise
        except _Refused as exc:
            answer = {**answer, "ok": False, "error": {"name": "Refused", "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001 — whatever the provider raised is the answer.
            answer = {**answer, "ok": False, "error": error_body(exc)}
        if type(rid) is int:
            with contextlib.suppress(Exception):  # the link may have dropped meanwhile
                await peer.send(answer)

    async def _run(self, peer: Peer, sid: str, op: str, frame: Mapping[str, Any]) -> None:
        if op in {"connect", "open"}:
            await self._bring_up(peer, sid, op, frame)
            return
        held = peer.sessions.get(sid)
        if op == "close":
            if held is not None:
                await self._discard(peer, sid)
            return
        if held is None:
            msg = "no such session"
            raise _Refused(msg)
        if op == "send":
            await held.real.send(
                TTSResponseSegment(
                    response_id=_text(frame.get("response_id"), 256),
                    playback_generation_id=_int(
                        frame.get("playback_generation_id"), low=0, high=2**31,
                    ),
                    sequence=_int(frame.get("sequence"), low=0, high=2**31),
                    text=_text(frame.get("text"), _MAX_SEGMENT_CHARS, empty=True),
                ),
            )
        elif op in {"finish", "abort"}:
            try:
                if op == "finish":
                    await held.real.finish()
                else:
                    await held.real.abort(_text(frame.get("reason"), 200, empty=True))
            finally:
                await self._discard(peer, sid)
        else:
            msg = f"unknown op {op!r}"
            raise _Refused(msg)

    async def _bring_up(self, peer: Peer, sid: str, op: str, frame: Mapping[str, Any]) -> None:
        """``connect`` or ``open``: create the session on first sight, and stream once open."""
        held = peer.sessions.get(sid) or self._create(peer, sid, frame.get("params"))
        try:
            if op == "connect":
                await held.real.connect()
            else:
                await held.real.open(
                    _text(frame.get("response_id"), 256),
                    _int(frame.get("playback_generation_id"), low=0, high=2**31),
                )
                held.pump = asyncio.create_task(self._pump(peer, sid, held.real))
        except BaseException:
            await self._discard(peer, sid)
            raise

    def _create(self, peer: Peer, sid: str, raw: object) -> _BrainSession:
        """The provider session a terminal's first ``connect`` or ``open`` names."""
        if not isinstance(raw, dict):
            msg = "params are required to create a session"
            raise _Refused(msg)
        if len(peer.sessions) >= MAX_SESSIONS:
            msg = f"a terminal holds at most {MAX_SESSIONS} provider sessions"
            raise _Refused(msg)
        language = raw.get("language")
        idle = raw.get("idle_close_s")
        if language not in lang.LANGUAGES or isinstance(idle, bool) or not isinstance(
            idle, int | float,
        ) or not 0 < idle <= _MAX_IDLE_CLOSE_S:
            msg = "params name an unknown language or an idle time out of range"
            raise _Refused(msg)
        index = _int(
            raw.get("endpoint_index"), low=0, high=self._provider.streaming_candidate_count - 1,
        )
        real = self._provider.create_tts_session(
            endpoint_index=index,
            language=language,
            idle_close_s=float(idle),
            command_queue_capacity=_int(raw.get("command_queue_capacity"), low=1, high=_MAX_QUEUE),
            audio_queue_capacity=_int(raw.get("audio_queue_capacity"), low=1, high=_MAX_QUEUE),
        )
        peer.sessions[sid] = held = _BrainSession(real)
        return held

    async def _pump(self, peer: Peer, sid: str, real: TTSSession) -> None:
        """Send the session's audio events, in order, then how the stream ended."""
        events = real.audio_events().__aiter__()
        n = 0
        while True:
            try:
                event = await events.__anext__()
                frame: dict[str, Any] = {"op": "audio", "n": n, "event": encode_event(event)}
                n += 1
            except StopAsyncIteration:
                frame = {"op": "end"}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the provider's failure is the terminal's to see.
                frame = {"op": "failed", "error": error_body(exc)}
            await peer.send({"type": "tts", "sid": sid, **frame})
            if frame["op"] != "audio":
                return

    async def _discard(self, peer: Peer, sid: str) -> None:
        """Forget a session and make sure its provider side is closed."""
        held = peer.sessions.pop(sid, None)
        if held is None:
            return
        if held.pump is not None and held.pump is not asyncio.current_task():
            held.pump.cancel()
            await asyncio.wait({held.pump})
        with contextlib.suppress(Exception):
            await asyncio.wait_for(held.real.close(), _DISCARD_TIMEOUT_S)


def _text(value: object, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or not (empty or value):
        msg = f"expected text of at most {limit} characters"
        raise _Refused(msg)
    return value
