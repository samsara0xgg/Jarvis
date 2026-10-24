"""ADR 0209: a paired phone's conversation socket, ``/phone/ws``.

The phone recognizes Allen's words itself and sends text; it plays her voice as the host's
native player, the ADR 0129 frames over this socket instead of a pipe
(:mod:`jarvis.surface.phone_player`). Only a paired device's token opens it, as on
``POST /inherent/device/events``: the local key is refused. One connection per device is live;
a newer one replaces the older (the older is closed with code 4000).

**JSON text frames** carry control::

    phone -> host   {"type": "hello", "voice": true}
    host -> phone   {"type": "ready", "device": "<paired name>", "voice": true,
                     "sample_rate": 32000, "judges": true}
    phone -> host   {"type": "say", "utterance_id": "<1-64 of A-Za-z0-9_->", "text": "...",
                     "spoken": true, "language": "en", "confidence": 0.93,
                     "about": {"kind": "reminder", "id": "...", "title": "...", "start_ms": 0},
                     "over_her": true, "conversation": false, "continues": "<utterance_id>",
                     "attachments": ["<id from POST /inherent/attachments>"], "topic": "t3"}
    host -> phone   {"type": "said", "utterance_id": "...", "turn_id": "T...",
                     "verdict": "turn", "record_id": "<32 hex>"}
    host -> phone   {"type": "row", "event_type": "surface.response_chunk",
                     "event_uid": "<32 hex>", "payload": {...}, "ts_epoch_ms": 1700000000000}
    host -> phone   {"type": "step", "turn_id": "T...", "step": 1, "tool": "bus",
                     "label": "...", "state": "running", "started_ms": 1700000000000,
                     "ended_ms": null}
    host -> phone   {"type": "reaction", "target": "<record id>", "by": "allen",
                     "emoji": "<one emoji>" or null}
    phone -> host   {"type": "barge"}                     it heard Allen over her voice, held
                                                          its output, and asks the host to stop
                                                          (the older phone; see ``over_her``)
    phone -> host   {"type": "cancel"}                    stop the current answer
    phone -> host   {"type": "ping", "t_ns": 123}
    host -> phone   {"type": "pong", "t_ns": 123, "host_ns": 456}
    host -> phone   {"type": "locate", "request_id": "<1-64 of A-Za-z0-9_->"}
    phone -> host   {"type": "located", "request_id": "...", "lat": 49.28, "lng": -123.12,
                     "accuracy_m": 12.0, "fix_ts_epoch_ms": 1700000000000,
                     "snapshot": "<id from POST /inherent/attachments>"}
                    or {"type": "located", "request_id": "...", "error": "denied"|"unavailable"}
    host -> phone   {"type": "error", "code": "bad_say", "message": "..."}

``voice`` in ``ready`` is false when the host has no speech key or could not build its voice
pipeline: the phone then gets rows and no audio. ``say`` with ``spoken: true`` is words the
phone heard; ``false`` is words typed, answered in rows only. The host derives the turn id from
the device and ``utterance_id``, so a resent ``say`` writes nothing twice and is answered with
the same ``said``. ``row`` carries the ``surface.response_*``, ``response.cancelled|failed`` and
``clarification.requested|withdrawn`` rows of the turns this device opened, voice and typed
alike, from the moment it connected (ADR 0215): the question card, the transit card's ``trip``
included, rides there and is never spoken; the phone reads the slot's current card with
``GET /inherent/clarification``, which its device token opens like every route but the liveness
probe. The final ``surface.response_emitted`` carries the written part (``document_text``,
``written_apart``) and the times the answer refers to (``times``).

**Steps (ADR 0231).** ``step`` is one tool call of a turn this device opened, voice or typed: it
goes out with ``state: "running"`` when the tool starts and again, with the same ``step`` number,
as ``done`` or ``failed`` with ``ended_ms`` set. ``step`` counts from 1 in the order the tools
started; ``tool`` is the tool's name; ``label`` is a fixed line of the language table, with a short
detail of the call's arguments after " · " for the few tools where it helps
(:mod:`jarvis.shared.steps`), and never the arguments themselves. ``started_ms`` and ``ended_ms``
are the host's clock. Steps and rows are sent in log order, so a tool's ``running`` precedes the
words it led to. The phone shows its own elapsed seconds; the steps of an answered turn come back,
summarized, on ``GET /inherent/conversation``.

A typed ``say`` (``spoken: false``) may carry ``attachments``, ids stored by ``POST
/inherent/attachments``: the turn is the one ``POST /inherent/submit`` makes with them, checked
by the same store (``error attachment_refused``, with the store's sentence, or ``no_attachments``
on a host without one) and, if ``text`` is blank, given the same filler words. Spoken words carry
none.

``say`` may carry ``about``, the item the phone has open (ADR 0214; ``jarvis.shared.about``): it
is kept on the turn's opening row, and one that is malformed is answered with ``error bad_say``.

``say`` may carry ``topic``, the id of the topic he is talking in (ADR 0390; ``GET
/inherent/topics``): the words and her answer are filed under it at once and her state names it.
A merged topic's id names the topic it went into, which the row keeps; an id that names no listed
topic, or a host that keeps none, is answered with ``error bad_say``.

**Judging words (ADR 0216).** ``judges`` in ``ready`` is true whenever the host can judge, which
the regexes alone do. A phone that hears Allen over her voice holds her audio itself and sends
no ``barge``; it sends the final words as a spoken ``say`` with ``over_her: true`` (and with
``conversation: true`` for a follow-up in its conversation mode) and the host judges them as the
Mac does (:mod:`jarvis.surface.word_judge`), answering ``said`` with a ``verdict``. A flag that is
present and not a bool is ``error bad_say``; typed words ignore both. ``turn``: with ``over_her``
the host first does what ``barge`` does, then records the say and answers its ``turn_id``.
``backchannel``, ``unclear``, ``echo``: nothing happens and ``turn_id`` is null, so the phone
resumes its held audio. ``stop``, ``wait``: the host does what ``barge`` does, no turn.
``dismissed``: what ``cancel`` does, no turn; the phone ends its own conversation mode.
``quiet:<level>``: that, then ``set_quiet(level)``; quiet commands are heard only where the host
wires ``set_quiet``. ``elsewhere``: another device recorded these words first (ADR 0219), so
nothing happens and ``turn_id`` is null; the phone does not resend them. ``not_owner``: the
same, for a long utterance that is not in his voice (ADR 0286; below). No turn is recorded
for any verdict but ``turn``. The judging runs beside
the receive loop (Jev may take seconds), so DISCARD_ACK keeps flowing. A resent flagged ``say``
is judged again, with no cache; a phone that gave up waiting resends it with no flags, which
records a turn, once. A ``say`` with no flag is a turn: ``verdict`` is ``"turn"``, unless the
host heard the words elsewhere first, which answers ``elsewhere`` flagged or not (ADR 0219);
typed words are never checked.

**His voice (ADR 0286).** A spoken ``say`` may carry ``voiceprint``: ``embedding`` (192 numbers),
``seconds`` of speech, ``mic`` (the name of the microphone that heard it) and ``mic_age_s`` (how
long that stream has been open). The host scores it against the profile of this device's
microphone, flagged or not, before anything else; a long one that scores far below it is
answered ``not_owner`` exactly as ``elsewhere`` is (nothing happens, ``turn_id`` null, no
resend), and a turn's row keeps ``voiceprint: {score, mic, seconds}``, never the embedding. A
``voiceprint`` that is malformed is ignored: the words are judged as without one. Typed words
carry none.

**Going on from a sentence (ADR 0271, 0278).** The host decides, by state, whether a spoken
``say`` goes on from this device's latest earlier spoken sentence: it does while her answer to
that one is not over (and it is under two minutes old), unless the words are a stop request or
a repeat of the earlier ones. ``continues`` is the ``utterance_id`` of the ``say`` the phone
believes it goes on from (a pause cut his sentence); a ``say`` carries only its new words,
never the earlier ones. The host composes the thought: the ``utterance.received`` it writes has
``transcript`` holding the earlier sentence's whole thought followed by the new words,
``segment`` the new words alone, ``continues`` the earlier turn and ``folded`` the record ids
whose words ``transcript`` holds; the earlier turn's unspoken audio is dropped, what is audible
stopped and its open answer cancelled as ``superseded`` naming this turn
(``interrupted_by_turn_id``), after a ``turn`` verdict when the ``say`` was judged. A
``continues`` is not needed, and when the host's pick is another turn it is ignored. When there
is no such earlier turn the words are a turn of their own. Typed words never continue; a value
that is not an id is ``error bad_say``.

**Reactions (ADR 0257).** ``reaction`` is one change of an emoji on a message: ``target`` is the
message's record id (the ``id`` of a row of ``GET /inherent/conversation``), ``by`` is ``allen`` or
``jarvis``, ``emoji`` is null when it was taken off. Unlike rows and steps it goes to every
connected phone, whichever device opened the turn, and in log order with them: his own taps on
another device, her ``<react>`` at the end of a turn and the eyes the program puts on a message
that arrived mid-turn. The newest frame per (``target``, ``by``) is the reaction now; the rows of
the conversation carry the same as ``reactions``. ``said`` with ``verdict: "turn"`` carries
``record_id``, the id the words will have as a message, so the phone can show a reaction on a line
it has just sent before any row has come back.

**A fresh fix (ADR 0281).** ``locate`` asks the phone for a fix now, because she needs to know
where he is. The phone answers ``located`` with the same ``request_id``: a fix taken after the
request, ``fix_ts_epoch_ms`` the time it was taken, and optionally ``snapshot``, the id of a
picture it stored with ``POST /inherent/attachments`` (a view from where he is), or ``error``:
``denied`` (no location permission) or ``unavailable`` (no fix could be taken). The host stores
a fix as an ordinary ``phone.location_observed`` event carrying ``request_id`` (and ``snapshot``)
and gives up waiting after a few seconds (``locate.socket_wait_s``), so a late answer is only a
location report. A ``located`` that is malformed is answered ``error bad_located``. A phone
with no live socket is asked by a background push instead (the push section of the spec).

**Binary frames** carry the player protocol, one WebSocket message per frame:
:mod:`jarvis.surface.phone_player` has the layouts. After ``ready`` with ``voice: true`` the
phone sends READY (its audio session is up); the host speaks nothing before it.

The host speaks no line back to a dismissal, 「等我一下」 or a quiet command as the Mac does
(:func:`jarvis.runtime.daemon.conversation_lines.say_conversation_line`); the phone shows its own.

Layer rules: stdlib, FastAPI and ``jarvis.surface`` only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from fastapi import WebSocketDisconnect

from jarvis.shared import lang
from jarvis.state.attachments import AttachmentRefused, valid_id
from jarvis.state.locate_wait import LOCATE_ERRORS, Refused, valid_request_id
from jarvis.state.phone_location import LOCATION_EVENT
from jarvis.state.voiceprint import Voiceprint
from jarvis.surface import word_judge
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneFrameError
from jarvis.surface.terminal_events import located_uid, phone_record_id, phone_turn_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from fastapi import WebSocket

    from jarvis.shared import Event
    from jarvis.state.voiceprint import Scored, VoiceprintBook
    from jarvis.surface.phone_player import PhoneAudioStreamPlayer
    from jarvis.surface.terminal_events import BrainEvents
    from jarvis.surface.terminal_voice import RowCursor

LOGGER = logging.getLogger("jarvis.surface.phone_link")

PHONE_PATH: Final = "/phone/ws"
HELLO_TIMEOUT_S: Final = 5.0
POLL_INTERVAL_S: Final = 0.05
"""How often the host looks for new rows of this device's turns, as its own watchers do."""
MAX_TEXT_CHARS: Final = 256 * 1024
"""One text frame's cap: a 20,000-character utterance escaped as JSON fits."""
REPLACED_CLOSE_CODE: Final = 4000
_REPLACE_WAIT_S: Final = 5.0
_RECENT_TURNS: Final = 8
_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


class PhoneVoice(Protocol):
    """One connection's speech: its player, the media actor around it and the actor's feed."""

    @property
    def player(self) -> PhoneAudioStreamPlayer:
        """The player whose socket this connection is."""

    async def begin(self) -> None:
        """The phone's READY arrived: start feeding the actor the device's spoken turns."""

    async def stop_playback(self, reason: str) -> str:
        """Stop what is audible now (the actor's interrupt, DISCARD, settled on its ack)."""

    async def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
        """Drop the answers of ``turn_ids`` none of which has reached the speaker."""

    async def close(self) -> None:
        """End the actor and its feed; the player's socket is already gone."""


class PhoneRows(Protocol):
    """Where a device's response rows come from; the runtime binds it to the event log."""

    def high_water(self) -> int:
        """The latest row's id (0 when the log is empty)."""

    def cursor(self, device: str, after: int, *, spoken_only: bool = False) -> RowCursor:
        """Rows with ``id > after`` of the turns ``device`` opened (``spoken_only``: by voice)."""

    def steps(self, device: str, after: int) -> StepCursor:
        """The ``step`` frames of the tools those turns ran, from the rows past ``after``."""

    def reactions(self, after: int) -> ReactionCursor:
        """The ``reaction`` frames of every reaction past ``after``, whichever device's turn."""

    def recent_turns(self, device: str, limit: int) -> list[str]:
        """The last ``limit`` turns ``device`` opened, oldest first, whatever socket it used."""


class StepCursor(Protocol):
    """A place in the action rows of one device's turns (ADR 0231)."""

    def poll(self) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
        """``(highest id seen, [(id of the row it came from, step frame)])``, advancing."""


class ReactionCursor(Protocol):
    """A place in the log's reaction events, which belong to no device (ADR 0257)."""

    def poll(self) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
        """``(highest id seen, [(id of the event, reaction frame)])``, advancing."""


type OpenVoice = Callable[[str, Callable[[bytes], bool]], Awaitable[PhoneVoice | None]]
"""``(device, send_binary) -> voice``: build one connection's speech, or ``None`` if the host
cannot speak. ``send_binary`` queues a binary frame for the socket from any thread."""


@dataclass
class PhoneHub:
    """What the phone route needs from the host; the runtime wires every field.

    ``barge_in(device)`` cancels the generation of the device's open answer under its
    interrupt policy; ``cancel_turn(turn_id, reason)`` cancels every open run of one turn.
    Both block (they open their own connections) and run on a worker thread.

    The words judge (ADR 0216) takes the Mac's hooks: ``ask_words``, ``note_words``,
    ``begin_line`` and ``recent_speech``, all of which block, and ``set_quiet(level)``, without
    which a quiet command is not recognized. Each is optional; the regexes alone are a judge.

    ``heard_elsewhere(device, turn_id, text)`` (ADR 0219) is whether another device recorded
    these words just now. It blocks and runs on a worker thread; true answers a spoken ``say``
    with ``elsewhere`` and records nothing.

    ``voiceprints`` (ADR 0286) is the owner's profiles: a spoken ``say`` that carries a
    ``voiceprint`` is scored against the profile of this device's microphone before anything
    else, and a long one that is not his is answered ``not_owner`` and records nothing.

    ``pick_earlier(device, turn_id, text)`` (ADR 0278) is the earlier turn of ``device`` a
    spoken ``say`` goes on from, or ``None``; ``supersede_turn(earlier_turn_id, turn_id)``
    (ADR 0271) takes the open answer of that turn, cancels it as superseded by ``turn_id`` and
    answers ``"superseded"``, or answers something else when that turn is over. Both block and
    run on a worker thread; without either, a ``say`` always stands alone.

    ``check_attachments(ids)`` (ADR 0231) is the store's check of the files a typed ``say``
    names, as ``POST /inherent/submit`` makes it: it returns, or raises
    :class:`~jarvis.state.attachments.AttachmentRefused`. It blocks and runs on a worker thread;
    ``None`` is a host that takes no attachments.

    ``resolve_topic(topic_id)`` (ADR 0390) is the id of the listed topic a ``say``'s ``topic``
    names now, or ``None``; ``None`` is a host that keeps no topics.
    """

    events: BrainEvents
    rows: PhoneRows
    open_voice: OpenVoice | None = None
    barge_in: Callable[[str], str] | None = None
    cancel_turn: Callable[[str, str], str] | None = None
    ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None
    note_words: Callable[[str, str, str, bool, bool], None] | None = None
    begin_line: Callable[[str, str, str, bool, bool], None] | None = None
    recent_speech: Callable[[], str] | None = None
    set_quiet: Callable[[str], None] | None = None
    heard_elsewhere: Callable[[str, str, str], bool] | None = None
    voiceprints: VoiceprintBook | None = None
    supersede_turn: Callable[[str, str], str] | None = None
    pick_earlier: Callable[[str, str, str], str | None] | None = None
    check_attachments: Callable[[Sequence[str]], object] | None = None
    resolve_topic: Callable[[str], str | None] | None = None
    live: dict[str, _Connection] = field(default_factory=dict)

    def connected(self, device: str) -> bool:
        """Whether ``device`` has a live socket now; the push sender reads it (ADR 0210)."""
        return device in self.live

    def ask_location(self, request_id: str, device: str | None = None) -> int:
        """Ask ``device`` (every connected phone when ``None``) for a fix now; how many were asked.

        The ``locate`` frame goes to that device's socket only (ADR 0322).

        Callable from any thread: ``where_am_i`` runs on a worker thread (ADR 0281).
        """
        frame = json.dumps({"type": "locate", "request_id": request_id})
        return sum(
            conn.queue_threadsafe(frame) for name, conn in list(self.live.items())
            if device is None or name == device
        )

    def word_hooks(
        self, heard_elsewhere: Callable[[str, str], bool] | None = None,
    ) -> word_judge.WordHooks:
        """The judge's view of this host; ``heard_elsewhere`` is one connection's check."""
        return word_judge.WordHooks(
            ask=self.ask_words, note=self.note_words, begin=self.begin_line,
            recent=self.recent_speech, quiet=self.set_quiet is not None,
            heard_elsewhere=heard_elsewhere,
        )


class _Connection:
    """One phone's socket: everything it sends goes through one queue, so frames keep order."""

    def __init__(self, device: str, ws: WebSocket) -> None:
        self.device = device
        self.ws = ws
        self.loop = asyncio.get_running_loop()
        self.outbox: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.done = asyncio.Event()
        self.voice: PhoneVoice | None = None
        self.began = False
        self.recorded_turns: deque[str] = deque(maxlen=_RECENT_TURNS)
        self.tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def send_json(self, frame: dict[str, Any]) -> None:
        """Queue a control frame; call from the loop thread."""
        if not self._closed:
            self.outbox.put_nowait(json.dumps(frame, ensure_ascii=False))

    def send_binary(self, data: bytes) -> bool:
        """Queue a binary frame from any thread; False once the connection is gone."""
        return self.queue_threadsafe(data)

    def queue_threadsafe(self, item: str | bytes) -> bool:
        """Queue a text or binary frame from any thread; False once the connection is gone."""
        if self._closed:
            return False
        try:
            self.loop.call_soon_threadsafe(self.outbox.put_nowait, item)
        except RuntimeError:  # the loop is closed
            return False
        return True

    def error(self, code: str, message: str) -> None:
        """Queue an ``error`` frame."""
        self.send_json({"type": "error", "code": code, "message": message})

    def spawn(self, work: Awaitable[None]) -> None:
        """Run ``work`` beside the receive loop, which must keep reading (DISCARD_ACK)."""
        task = asyncio.ensure_future(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def mark_closed(self) -> None:
        """Stop accepting frames for the socket."""
        self._closed = True

    async def pump(self) -> None:
        """Send the queue's frames in order until the socket fails or the task is cancelled."""
        try:
            while True:
                item = await self.outbox.get()
                if isinstance(item, bytes):
                    await self.ws.send_bytes(item)
                else:
                    await self.ws.send_text(item)
        except (WebSocketDisconnect, RuntimeError):
            return


def _hello_voice(text: str) -> bool | None:
    """Whether the hello declares voice, or ``None`` if the frame is not a hello."""
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if not isinstance(frame, dict) or frame.get("type") != "hello":
        return None
    voice = frame.get("voice", False)
    return voice if isinstance(voice, bool) else None


async def serve_phone(hub: PhoneHub, ws: WebSocket, device: str) -> None:
    """The host's end of one phone's socket, from accept to disconnect.

    ``device`` is the paired name the caller's token belongs to; the caller has already
    checked the token. A first frame that is not a hello within a few seconds closes the socket.
    """
    await ws.accept()
    try:
        message = await asyncio.wait_for(ws.receive(), HELLO_TIMEOUT_S)
    except (TimeoutError, WebSocketDisconnect):
        message = {}
    wants_voice = _hello_voice(message["text"]) if isinstance(message.get("text"), str) else None
    if wants_voice is None:
        LOGGER.info("phone %s: closed before a hello", device)
        with contextlib.suppress(RuntimeError):
            await ws.close(code=1008)
        return
    old = hub.live.get(device)
    if old is not None:
        with contextlib.suppress(RuntimeError):
            await old.ws.close(code=REPLACED_CLOSE_CODE)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(old.done.wait(), _REPLACE_WAIT_S)
    conn = _Connection(device, ws)
    hub.live[device] = conn
    pump = asyncio.create_task(conn.pump())
    streaming: asyncio.Task[None] | None = None
    try:
        if wants_voice and hub.open_voice is not None:
            try:
                conn.voice = await hub.open_voice(device, conn.send_binary)
            except Exception:
                LOGGER.exception("phone %s: the voice pipeline could not be built", device)
        streaming = asyncio.create_task(_stream_rows(hub, conn))
        LOGGER.info("phone %s: connected (voice=%s)", device, conn.voice is not None)
        conn.send_json(
            {
                "type": "ready", "device": device, "voice": conn.voice is not None,
                "sample_rate": PHONE_SAMPLE_RATE_HZ, "judges": True,
            },
        )
        await _receive(hub, conn)
    except (WebSocketDisconnect, RuntimeError):
        pass  # the phone left, or this socket was closed under us by a replacing connection
    finally:
        await _end(hub, conn, pump, streaming)


async def _end(
    hub: PhoneHub, conn: _Connection, pump: asyncio.Task[None],
    streaming: asyncio.Task[None] | None,
) -> None:
    """Stop everything one connection started, then let a replacing connection go on."""
    conn.mark_closed()
    if conn.voice is not None:
        conn.voice.player.connection_lost()
    for task in [pump, *([streaming] if streaming else []), *conn.tasks]:
        task.cancel()
    await asyncio.gather(
        pump, *([streaming] if streaming else []), *conn.tasks, return_exceptions=True,
    )
    if conn.voice is not None:
        try:
            await conn.voice.close()
        except Exception:
            LOGGER.exception("phone %s: closing its voice pipeline failed", conn.device)
    if hub.live.get(conn.device) is conn:
        del hub.live[conn.device]
    with contextlib.suppress(RuntimeError):
        await conn.ws.close()
    LOGGER.info("phone %s: disconnected", conn.device)
    conn.done.set()


async def _stream_rows(hub: PhoneHub, conn: _Connection) -> None:
    """Send the rows and the steps of this device's turns, in log order, from where it connected.

    Rows are read before steps, and the two merged by log id: a step whose action row came
    before a row read now is read now too, so its ``running`` never trails the words it led to.
    """
    start = hub.rows.high_water()
    cursor = hub.rows.cursor(conn.device, start)
    steps = hub.rows.steps(conn.device, start)
    reacted = hub.rows.reactions(start)
    while True:
        batch: list[tuple[int, Event]] = []
        stepped: list[tuple[int, dict[str, Any]]] = []
        reactions: list[tuple[int, dict[str, Any]]] = []
        try:
            _after, batch = cursor.poll()
        except sqlite3.Error:
            LOGGER.exception("phone %s: reading its rows failed; retrying", conn.device)
        try:
            _seen, stepped = steps.poll()
        except sqlite3.Error:
            LOGGER.exception("phone %s: reading its steps failed; retrying", conn.device)
        try:
            _seen, reactions = reacted.poll()
        except sqlite3.Error:
            LOGGER.exception("phone %s: reading the reactions failed; retrying", conn.device)
        frames = [
            (row_id, {
                "type": "row", "event_type": event.type, "event_uid": event.event_uid,
                "payload": dict(event.payload), "ts_epoch_ms": event.ts_epoch_ms,
            })
            for row_id, event in batch
        ]
        for _row_id, frame in sorted([*frames, *stepped, *reactions], key=lambda one: one[0]):
            conn.send_json(frame)
        await asyncio.sleep(POLL_INTERVAL_S)


async def _receive(hub: PhoneHub, conn: _Connection) -> None:
    while True:
        message = await conn.ws.receive()
        if message["type"] == "websocket.disconnect":
            return
        text, data = message.get("text"), message.get("bytes")
        if isinstance(data, bytes):
            if not await _binary(conn, data):
                return
        elif isinstance(text, str):
            if len(text) > MAX_TEXT_CHARS:
                await conn.ws.close(code=1009)
                return
            _control(hub, conn, text)


async def _binary(conn: _Connection, data: bytes) -> bool:
    """One player frame from the phone; False closes the socket."""
    voice = conn.voice
    if voice is None:
        conn.error("no_voice", "this connection has no voice")
        return True
    try:
        voice.player.on_binary(data)
    except PhoneFrameError as exc:
        conn.error("bad_frame", str(exc))
        with contextlib.suppress(RuntimeError):
            await conn.ws.close(code=1003)
        return False
    if not conn.began and voice.player.is_running:
        conn.began = True
        await voice.begin()
    return True


def _control(hub: PhoneHub, conn: _Connection, text: str) -> None:
    """One JSON control frame from the phone; anything it does not understand is ignored."""
    try:
        frame = json.loads(text)
    except ValueError:
        return
    kind = frame.get("type") if isinstance(frame, dict) else None
    if kind == "say":
        _say(hub, conn, frame)
    elif kind == "ping":
        t_ns = frame.get("t_ns")
        if type(t_ns) is not int:
            conn.error("bad_ping", "ping needs an integer t_ns")
            return
        host_ns = time.monotonic_ns()
        if conn.voice is not None:
            conn.voice.player.observe_clock(t_ns, host_ns)
        conn.send_json({"type": "pong", "t_ns": t_ns, "host_ns": host_ns})
    elif kind == "located":
        _located(hub, conn, frame)
    elif kind == "barge":
        conn.spawn(_interrupt(hub, conn, reason="barge_in", cancel_turns=False))
    elif kind == "cancel":
        conn.spawn(_interrupt(hub, conn, reason="user_stop", cancel_turns=True))


def _located(hub: PhoneHub, conn: _Connection, frame: dict[str, Any]) -> None:
    """The phone's answer to a ``locate`` (ADR 0281): store the fix, or pass on why there is none.

    A fix is appended as a ``phone.location_observed`` carrying the request's id, by the same
    path as a phone's report, which also answers the request that waits for it. A request
    nobody waits for any more is only a report.
    """
    request_id, error = frame.get("request_id"), frame.get("error")
    waits = hub.events.locates
    if not valid_request_id(request_id):
        conn.error("bad_located", "located needs a request_id")
        return
    if not (error is None or error in LOCATE_ERRORS):
        conn.error("bad_located", "located's error is denied or unavailable")
        return
    if error is not None:
        if waits is not None:
            waits.deliver(request_id, Refused(error, conn.device))
        return
    payload = {
        "lat": frame.get("lat"), "lng": frame.get("lng"), "accuracy_m": frame.get("accuracy_m"),
        "request_id": request_id,
    }
    for key in ("snapshot", "place"):
        if frame.get(key) is not None:
            payload[key] = frame[key]
    event = {
        "event_uid": located_uid(conn.device, request_id), "event_type": LOCATION_EVENT,
        "ts_epoch_ms": frame.get("fix_ts_epoch_ms"), "payload": payload,
    }
    try:
        ack = hub.events.record_phone_batch(conn.device, [event])[0]
    except sqlite3.Error:
        LOGGER.exception("phone %s: appending its fix failed", conn.device)
        ack = {"ok": False, "code": "retry"}
    if not ack["ok"]:
        conn.error("bad_located", f"the fix was refused: {ack.get('code')}")
        if waits is not None:
            waits.deliver(request_id, Refused("unavailable", conn.device))


@dataclass(frozen=True)
class _Say:
    """One ``say`` frame, checked."""

    utterance_id: str
    text: str
    spoken: bool
    language: str | None
    confidence: Any
    about: Any
    over_her: bool
    conversation: bool
    attachments: tuple[str, ...] = ()
    continues: str | None = None
    voiceprint: Voiceprint | None = None
    topic: str | None = None

    @property
    def judged(self) -> bool:
        """Spoken words the phone asks the host to judge; typed words ignore the flags."""
        return self.spoken and (self.over_her or self.conversation)


def _say(hub: PhoneHub, conn: _Connection, frame: dict[str, Any]) -> None:
    utterance_id, text, spoken = frame.get("utterance_id"), frame.get("text"), frame.get("spoken")
    language, confidence = frame.get("language"), frame.get("confidence")
    over_her, conversation = frame.get("over_her", False), frame.get("conversation", False)
    if (
        not isinstance(utterance_id, str) or _ID.fullmatch(utterance_id) is None
        or not isinstance(text, str) or not isinstance(spoken, bool)
        or not (language is None or isinstance(language, str))
    ):
        conn.error("bad_say", "say needs utterance_id, text and spoken")
        return
    if not isinstance(over_her, bool) or not isinstance(conversation, bool):
        conn.error("bad_say", "say's over_her and conversation are true or false")
        return
    continues = frame.get("continues")
    if continues is not None and not (
        isinstance(continues, str) and _ID.fullmatch(continues)
    ):
        conn.error("bad_say", "say's continues is the utterance_id of an earlier say")
        return
    files = frame.get("attachments", [])
    if (
        not isinstance(files, list) or not all(valid_id(one) for one in files)
        or (files and spoken)
    ):
        conn.error("bad_say", "say's attachments are the ids of files, and go with typed words")
        return
    if files and hub.check_attachments is None:
        conn.error("no_attachments", "this host takes no attachments")
        return
    if files and not text.strip():
        text = lang.t("attach.no_words")
    topic = frame.get("topic")
    if topic is not None:
        topic = _listed_topic(hub, topic)
        if topic is None:
            conn.error("bad_say", "say's topic is the id of a topic this host lists")
            return
    say = _Say(
        utterance_id, text, spoken, language, confidence, frame.get("about"),
        over_her, conversation, tuple(dict.fromkeys(files)),
        continues if spoken and continues != utterance_id else None,  # typed words never continue
        Voiceprint.parse(frame.get("voiceprint")) if spoken else None,
        topic,
    )
    _route_say(hub, conn, say)


def _listed_topic(hub: PhoneHub, topic: object) -> str | None:
    """The listed topic a ``say``'s ``topic`` names now (ADR 0390), else ``None``.

    One indexed read of the topics' small table, on the receive loop as the say's own write is.
    """
    if hub.resolve_topic is None or not isinstance(topic, str) or _ID.fullmatch(topic) is None:
        return None
    try:
        return hub.resolve_topic(topic)
    except sqlite3.Error:
        LOGGER.exception("phone: reading the topics failed; the say is refused")
        return None


def _route_say(hub: PhoneHub, conn: _Connection, say: _Say) -> None:
    """Where a checked ``say`` goes: its files, the judge, a check, a take-over, or the log."""
    if say.attachments:
        conn.spawn(_record_with_files(hub, conn, say))
    elif say.judged:
        conn.spawn(_judge_say(hub, conn, say))
    elif say.spoken and (
        (check := _elsewhere_check(hub, conn)) is not None
        or (hub.voiceprints is not None and say.voiceprint is not None)
    ):
        conn.spawn(_record_unless_dropped(hub, conn, say, check))
    elif say.spoken and hub.pick_earlier is not None and hub.supersede_turn is not None:
        conn.spawn(_record_continuing(hub, conn, say))
    else:
        _record_say(hub, conn, say)


async def _record_with_files(hub: PhoneHub, conn: _Connection, say: _Say) -> None:
    """A typed ``say`` with files is a turn once the store has vouched for them (ADR 0231).

    The store is read on a worker thread, so the receive loop never blocks on the disk; a say
    already recorded is not checked again.
    """
    if hub.check_attachments is not None and not hub.events.has_phone_say(
        conn.device, say.utterance_id,
    ):  # a resend of a recorded say is answered with its turn, whatever became of the files
        try:
            await asyncio.to_thread(hub.check_attachments, say.attachments)
        except AttachmentRefused as exc:
            conn.error("attachment_refused", str(exc))
            return
    _record_say(hub, conn, say)


def _elsewhere_check(hub: PhoneHub, conn: _Connection) -> Callable[[str, str], bool] | None:
    """``(turn_id, text) -> bool`` for this phone: another device just recorded those words.

    A ``say`` this connection already recorded is never absorbed by a device that came after
    it, so a resent one is answered with the turn it made (ADR 0219).
    """
    ask = hub.heard_elsewhere
    if ask is None:
        return None

    def check(turn_id: str, text: str) -> bool:
        return turn_id not in conn.recorded_turns and ask(conn.device, turn_id, text)

    return check


def _answer_dropped(conn: _Connection, say: _Say, verdict: str) -> None:
    LOGGER.info("phone %s: words dropped (%s); no turn", conn.device, verdict)
    conn.send_json(
        {"type": "said", "utterance_id": say.utterance_id, "turn_id": None, "verdict": verdict},
    )


async def _score_say(hub: PhoneHub, conn: _Connection, say: _Say) -> Scored | None:
    """The spoken ``say``'s voiceprint scored on this phone's microphone (ADR 0286).

    ``None`` without a voiceprint or profiles, when scoring fails, and for a ``say`` this
    connection already recorded: a resend keeps the turn it made.
    """
    book = hub.voiceprints
    if book is None or say.voiceprint is None or hub.events.has_phone_say(
        conn.device, say.utterance_id,
    ):
        return None
    try:
        return await asyncio.to_thread(book.score, conn.device, say.voiceprint)
    except Exception:  # noqa: BLE001 - a profile cannot lose the phone's words
        LOGGER.warning("phone %s: scoring its voiceprint failed; the say is judged as it is",
                       conn.device, exc_info=True)
        return None


async def _finish_say(hub: PhoneHub, conn: _Connection, scored: Scored | None, verdict: str,
                      say: _Say) -> None:
    """Log the scored ``say`` with its verdict; a turn of his own teaches his profile."""
    if scored is None or hub.voiceprints is None:
        return
    try:
        await asyncio.to_thread(
            hub.voiceprints.finish, scored, verdict, phone_turn_id(conn.device, say.utterance_id),
        )
    except Exception:  # noqa: BLE001 - a profile cannot lose the phone's words
        LOGGER.warning("phone %s: learning from its voiceprint failed", conn.device,
                       exc_info=True)


async def _record_unless_dropped(
    hub: PhoneHub, conn: _Connection, say: _Say,
    check: Callable[[str, str], bool] | None,
) -> None:
    """An unflagged spoken ``say`` is a turn unless it is not his voice or was heard elsewhere.

    The voiceprint is asked first (ADR 0286), then the other devices' log (ADR 0219), each on
    a worker thread, so the receive loop never blocks on SQLite.
    """
    scored = await _score_say(hub, conn, say)
    verdict = "not_owner" if scored is not None and scored.turn_away else "turn"
    if verdict == "turn" and check is not None:
        try:
            elsewhere = await asyncio.to_thread(
                check, phone_turn_id(conn.device, say.utterance_id), say.text,
            )
        except Exception:  # noqa: BLE001 - another device's log cannot lose the phone's words
            LOGGER.warning("phone %s: heard_elsewhere failed; the say stays a turn",
                           conn.device, exc_info=True)
            elsewhere = False
        verdict = "elsewhere" if elsewhere else "turn"
    if verdict == "turn":
        await _record_continuing(hub, conn, say, scored)
    else:
        _answer_dropped(conn, say, verdict)
    await _finish_say(hub, conn, scored, verdict, say)


def _record_say(
    hub: PhoneHub, conn: _Connection, say: _Say, continues: str | None = None,
    scored: Scored | None = None,
) -> None:
    """Write the words as a turn, once, and answer ``said`` with its id.

    ``continues`` is the earlier turn they were composed into (ADR 0278); ``scored`` the
    score of their voiceprint, which the row keeps (ADR 0286).
    """
    try:
        turn_id = hub.events.record_phone_say(
            conn.device, say.utterance_id, say.text, spoken=say.spoken, language=say.language,
            confidence=say.confidence, about=say.about, attachments=say.attachments,
            continues=continues, voiceprint=None if scored is None else scored.payload(),
            topic=say.topic,
        )
    except ValueError as exc:
        LOGGER.warning("phone %s: say refused: %s", conn.device, exc)
        conn.error("bad_say", str(exc))
        return
    except sqlite3.Error:
        LOGGER.exception("phone %s: appending its utterance failed", conn.device)
        conn.error("retry", "the host could not record this; send it again")
        return
    if turn_id not in conn.recorded_turns:
        conn.recorded_turns.append(turn_id)
    conn.send_json(
        {
            "type": "said", "utterance_id": say.utterance_id, "turn_id": turn_id,
            "verdict": "turn", "record_id": phone_record_id(conn.device, say.utterance_id),
        },
    )


async def _record_continuing(
    hub: PhoneHub, conn: _Connection, say: _Say, scored: Scored | None = None,
) -> None:
    """Record a spoken ``say``, first taking over from the sentence it goes on from (ADR 0278)."""
    _record_say(hub, conn, say, await _take_over(hub, conn, say), scored)


async def _take_over(hub: PhoneHub, conn: _Connection, say: _Say) -> str | None:
    """The earlier turn ``say`` is composed into, once its answer is taken over; else ``None``.

    The host decides by state which turn that is (``pick_earlier``, ADR 0278); a ``continues``
    the phone sent is accepted when it names that turn and dropped when it does not. The
    earlier turn's unspoken audio is dropped, what is audible stopped, and its open answer
    cancelled as superseded by this turn; the words are then recorded as both sentences in
    one. When there is no such turn the words stand alone. A resent ``say`` that was recorded
    is not taken over again.
    """
    events = hub.events
    if (
        not say.spoken or hub.pick_earlier is None or hub.supersede_turn is None
        or events.has_phone_say(conn.device, say.utterance_id)
    ):  # a resend: the first one's turn is the answer, nothing more to take over
        return None
    turn_id = phone_turn_id(conn.device, say.utterance_id)
    try:
        earlier = await asyncio.to_thread(hub.pick_earlier, conn.device, turn_id, say.text)
    except Exception:  # noqa: BLE001 - what he said is never lost to a failed pick
        LOGGER.warning("phone %s: picking the sentence it goes on from failed", conn.device,
                       exc_info=True)
        return None
    if earlier is None or (
        say.continues is not None and earlier != phone_turn_id(conn.device, say.continues)
    ) or events.continued_words(conn.device, earlier, say.text) is None:
        return None
    outcome = "failed"
    try:
        if conn.voice is not None:
            await conn.voice.drop_unspoken(frozenset({earlier}))
            await conn.voice.stop_playback("barge_in")
        outcome = await asyncio.to_thread(hub.supersede_turn, earlier, turn_id)
    except Exception:
        LOGGER.exception("phone %s: taking over from %s failed", conn.device, earlier)
        conn.error("interrupt_failed", "taking over from the earlier sentence did not complete")
    LOGGER.info("phone %s: words go on from %s -> %s", conn.device, earlier, outcome)
    return earlier if outcome == "superseded" else None


async def _judge_say(hub: PhoneHub, conn: _Connection, say: _Say) -> None:
    """Judge words the phone heard over her or in its conversation mode, as the Mac does.

    The verdict runs on a worker thread (Jev may take seconds) beside the receive loop. A judge
    that fails leaves the words a turn. See the module docstring for what each verdict does.
    """
    turn_id = phone_turn_id(conn.device, say.utterance_id)
    scored = await _score_say(hub, conn, say)
    try:
        verdict = await asyncio.to_thread(
            word_judge.words_verdict, hub.word_hooks(_elsewhere_check(hub, conn)), turn_id,
            say.text,
            conversation=say.conversation, over_her=say.over_her,
            not_owner=scored is not None and scored.turn_away,
        )
    except Exception:
        LOGGER.exception("phone %s: judging its words failed; they stay a turn", conn.device)
        verdict = "turn"
    LOGGER.info("phone %s: words over_her=%s conversation=%s -> %s",
                conn.device, say.over_her, say.conversation, verdict)
    await _finish_say(hub, conn, scored, verdict, say)
    if verdict == "turn":
        earlier = await _take_over(hub, conn, say)
        if say.over_her and earlier is None:
            await _interrupt(hub, conn, reason="barge_in", cancel_turns=False)
        _record_say(hub, conn, say, earlier, scored)
        return
    if verdict in {"stop", "wait"}:
        await _interrupt(hub, conn, reason="barge_in", cancel_turns=False)
    elif verdict == "dismissed" or verdict.startswith("quiet:"):
        await _interrupt(hub, conn, reason="user_stop", cancel_turns=True)
        if verdict.startswith("quiet:") and hub.set_quiet is not None:
            try:
                await asyncio.to_thread(hub.set_quiet, verdict.removeprefix("quiet:"))
            except Exception:
                LOGGER.exception("phone %s: setting quiet failed", conn.device)
                conn.error("quiet_failed", "the quiet level was not set")
    # backchannel, unclear, echo, elsewhere, not_owner: nothing happens and the phone goes on
    # with her audio
    conn.send_json(
        {"type": "said", "utterance_id": say.utterance_id, "turn_id": None, "verdict": verdict},
    )


async def _interrupt(
    hub: PhoneHub, conn: _Connection, *, reason: str, cancel_turns: bool,
) -> None:
    """The local path's interrupt for this device: stop the sound, then the answer.

    ``barge`` is Allen talking over her: the actor's playback stops (a DISCARD to the phone,
    settled on its DISCARD_ACK) and the open answer's generation is cancelled under its policy.
    ``cancel`` is the stop button: the same, for every recent turn of this device, found in the
    log by the device named on the turn's opening row, not by the socket that carried it: a turn
    the phone opened before it reconnected, or over HTTP, is stopped too.
    """
    try:
        if conn.voice is not None:
            await conn.voice.stop_playback(reason)
        if cancel_turns and hub.cancel_turn is not None:
            for turn_id in hub.rows.recent_turns(conn.device, _RECENT_TURNS):
                await asyncio.to_thread(hub.cancel_turn, turn_id, reason)
        elif hub.barge_in is not None:
            outcome = await asyncio.to_thread(hub.barge_in, conn.device)
            LOGGER.info("phone %s barge-in: generation=%s", conn.device, outcome)
    except Exception:
        LOGGER.exception("phone %s: %s failed", conn.device, reason)
        conn.error("interrupt_failed", f"{reason} did not complete")
