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
                     "over_her": true, "conversation": false}
    host -> phone   {"type": "said", "utterance_id": "...", "turn_id": "T...",
                     "verdict": "turn"}
    host -> phone   {"type": "row", "event_type": "surface.response_chunk",
                     "event_uid": "<32 hex>", "payload": {...}, "ts_epoch_ms": 1700000000000}
    phone -> host   {"type": "barge"}                     it heard Allen over her voice, held
                                                          its output, and asks the host to stop
                                                          (the older phone; see ``over_her``)
    phone -> host   {"type": "cancel"}                    stop the current answer
    phone -> host   {"type": "ping", "t_ns": 123}
    host -> phone   {"type": "pong", "t_ns": 123, "host_ns": 456}
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

``say`` may carry ``about``, the item the phone has open (ADR 0214; ``jarvis.shared.about``): it
is kept on the turn's opening row, and one that is malformed is answered with ``error bad_say``.

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
nothing happens and ``turn_id`` is null; the phone does not resend them. No turn is recorded
for any verdict but ``turn``. The judging runs beside
the receive loop (Jev may take seconds), so DISCARD_ACK keeps flowing. A resent flagged ``say``
is judged again, with no cache; a phone that gave up waiting resends it with no flags, which
records a turn, once. A ``say`` with no flag is a turn: ``verdict`` is ``"turn"``, unless the
host heard the words elsewhere first, which answers ``elsewhere`` flagged or not (ADR 0219);
typed words are never checked.

**Binary frames** carry the player protocol, one WebSocket message per frame:
:mod:`jarvis.surface.phone_player` has the layouts. After ``ready`` with ``voice: true`` the
phone sends READY (its audio session is up); the host speaks nothing before it.

The host speaks no line back to a dismissal, 「等我一下」 or a quiet command as the Mac does
(:func:`jarvis.runtime.inherent_loop._say_conversation_line`); the phone shows its own.

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

from jarvis.surface import word_judge
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneFrameError
from jarvis.surface.terminal_events import phone_turn_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import WebSocket

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

    async def close(self) -> None:
        """End the actor and its feed; the player's socket is already gone."""


class PhoneRows(Protocol):
    """Where a device's response rows come from; the runtime binds it to the event log."""

    def high_water(self) -> int:
        """The latest row's id (0 when the log is empty)."""

    def cursor(self, device: str, after: int, *, spoken_only: bool = False) -> RowCursor:
        """Rows with ``id > after`` of the turns ``device`` opened (``spoken_only``: by voice)."""


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
    live: dict[str, _Connection] = field(default_factory=dict)

    def connected(self, device: str) -> bool:
        """Whether ``device`` has a live socket now; the push sender reads it (ADR 0210)."""
        return device in self.live

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
        self.recent_turns: deque[str] = deque(maxlen=_RECENT_TURNS)
        self.tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def send_json(self, frame: dict[str, Any]) -> None:
        """Queue a control frame; call from the loop thread."""
        if not self._closed:
            self.outbox.put_nowait(json.dumps(frame, ensure_ascii=False))

    def send_binary(self, data: bytes) -> bool:
        """Queue a binary frame from any thread; False once the connection is gone."""
        if self._closed:
            return False
        try:
            self.loop.call_soon_threadsafe(self.outbox.put_nowait, data)
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
    """Send the response rows of this device's turns, in log order, from where it connected."""
    cursor = hub.rows.cursor(conn.device, hub.rows.high_water())
    while True:
        try:
            _after, batch = cursor.poll()
        except sqlite3.Error:
            LOGGER.exception("phone %s: reading its rows failed; retrying", conn.device)
            batch = []
        for _row_id, event in batch:
            conn.send_json(
                {
                    "type": "row", "event_type": event.type, "event_uid": event.event_uid,
                    "payload": dict(event.payload), "ts_epoch_ms": event.ts_epoch_ms,
                },
            )
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
    elif kind == "barge":
        conn.spawn(_interrupt(hub, conn, reason="barge_in", cancel_turns=False))
    elif kind == "cancel":
        conn.spawn(_interrupt(hub, conn, reason="user_stop", cancel_turns=True))


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
    say = _Say(
        utterance_id, text, spoken, language, confidence, frame.get("about"),
        over_her, conversation,
    )
    if say.judged:
        conn.spawn(_judge_say(hub, conn, say))
    elif say.spoken and (check := _elsewhere_check(hub, conn)) is not None:
        conn.spawn(_record_unless_elsewhere(hub, conn, say, check))
    else:
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
        return turn_id not in conn.recent_turns and ask(conn.device, turn_id, text)

    return check


def _answer_elsewhere(conn: _Connection, say: _Say) -> None:
    LOGGER.info("phone %s: words heard elsewhere first; no turn", conn.device)
    conn.send_json(
        {"type": "said", "utterance_id": say.utterance_id, "turn_id": None,
         "verdict": "elsewhere"},
    )


async def _record_unless_elsewhere(
    hub: PhoneHub, conn: _Connection, say: _Say, check: Callable[[str, str], bool],
) -> None:
    """An unflagged spoken ``say`` is a turn unless another device recorded the words first.

    The log is read on a worker thread, so the receive loop never blocks on SQLite.
    """
    try:
        elsewhere = await asyncio.to_thread(
            check, phone_turn_id(conn.device, say.utterance_id), say.text,
        )
    except Exception:  # noqa: BLE001 - another device's log cannot lose the phone's words
        LOGGER.warning("phone %s: heard_elsewhere failed; the say stays a turn",
                       conn.device, exc_info=True)
        elsewhere = False
    if elsewhere:
        _answer_elsewhere(conn, say)
    else:
        _record_say(hub, conn, say)


def _record_say(hub: PhoneHub, conn: _Connection, say: _Say) -> None:
    """Write the words as a turn, once, and answer ``said`` with its id."""
    try:
        turn_id = hub.events.record_phone_say(
            conn.device, say.utterance_id, say.text, spoken=say.spoken, language=say.language,
            confidence=say.confidence, about=say.about,
        )
    except ValueError as exc:
        LOGGER.warning("phone %s: say refused: %s", conn.device, exc)
        conn.error("bad_say", str(exc))
        return
    except sqlite3.Error:
        LOGGER.exception("phone %s: appending its utterance failed", conn.device)
        conn.error("retry", "the host could not record this; send it again")
        return
    if turn_id not in conn.recent_turns:
        conn.recent_turns.append(turn_id)
    conn.send_json(
        {"type": "said", "utterance_id": say.utterance_id, "turn_id": turn_id, "verdict": "turn"},
    )


async def _judge_say(hub: PhoneHub, conn: _Connection, say: _Say) -> None:
    """Judge words the phone heard over her or in its conversation mode, as the Mac does.

    The verdict runs on a worker thread (Jev may take seconds) beside the receive loop. A judge
    that fails leaves the words a turn. See the module docstring for what each verdict does.
    """
    turn_id = phone_turn_id(conn.device, say.utterance_id)
    try:
        verdict = await asyncio.to_thread(
            word_judge.words_verdict, hub.word_hooks(_elsewhere_check(hub, conn)), turn_id,
            say.text,
            conversation=say.conversation, over_her=say.over_her,
        )
    except Exception:
        LOGGER.exception("phone %s: judging its words failed; they stay a turn", conn.device)
        verdict = "turn"
    LOGGER.info("phone %s: words over_her=%s conversation=%s -> %s",
                conn.device, say.over_her, say.conversation, verdict)
    if verdict == "turn":
        if say.over_her:
            await _interrupt(hub, conn, reason="barge_in", cancel_turns=False)
        _record_say(hub, conn, say)
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
    # backchannel, unclear, echo, elsewhere: nothing happens and the phone goes on with her audio
    conn.send_json(
        {"type": "said", "utterance_id": say.utterance_id, "turn_id": None, "verdict": verdict},
    )


async def _interrupt(
    hub: PhoneHub, conn: _Connection, *, reason: str, cancel_turns: bool,
) -> None:
    """The local path's interrupt for this device: stop the sound, then the answer.

    ``barge`` is Allen talking over her: the actor's playback stops (a DISCARD to the phone,
    settled on its DISCARD_ACK) and the open answer's generation is cancelled under its policy.
    ``cancel`` is the stop button: the same, for every recent turn of this device.
    """
    try:
        if conn.voice is not None:
            await conn.voice.stop_playback(reason)
        if cancel_turns and hub.cancel_turn is not None:
            for turn_id in list(conn.recent_turns):
                await asyncio.to_thread(hub.cancel_turn, turn_id, reason)
        elif hub.barge_in is not None:
            outcome = await asyncio.to_thread(hub.barge_in, conn.device)
            LOGGER.info("phone %s barge-in: generation=%s", conn.device, outcome)
    except Exception:
        LOGGER.exception("phone %s: %s failed", conn.device, reason)
        conn.error("interrupt_failed", f"{reason} did not complete")
