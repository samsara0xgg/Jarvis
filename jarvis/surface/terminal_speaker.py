"""ADR 0172: a voice terminal's end of speech over the brain link.

The terminal runs the media actor (player, ledger, ducking, cues, the ``say`` fallback)
unchanged and crosses the link at its two edges, which :mod:`jarvis.surface.terminal_voice`
describes on the wire:

* **The answer** arrives as rows. :class:`Journal` is a scratch event log in the terminal's
  runtime root, emptied at every start, that holds them; the media actor reads it as it reads
  the daemon's log.
* **The provider** is :class:`RemoteTTSProvider`, whose sessions proxy each ``TTSSession``
  call to the brain, which holds the key.

What the media actor writes into the journal about playback goes back to the brain as events
(:func:`forward_playback`). The capture session's calls into the brain's state ride the same
link as ``ask`` frames (:meth:`VoiceLink.ask`, :meth:`VoiceLink.tell`).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import Event
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import EventLogError, emit_event, open_event_log, open_runtime_event_log
from jarvis.surface.terminal_events import PLAYBACK_EVENT_TYPES
from jarvis.surface.terminal_voice import HEX_ID, RESPONSE_ROW_TYPES, decode_event
from jarvis.surface.voice_tts import TTSResponseSegment, TTSSessionClosedError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

    from jarvis.shared.lang import Language
    from jarvis.surface.terminal_events import EventOutbox
    from jarvis.surface.voice_tts import TTSAudioEvent

LOGGER = logging.getLogger("jarvis.surface.terminal_speaker")

REMOTE_ENDPOINTS: Final = 2
"""The endpoints a provider session may name: the real client's primary and fallback. The
brain refuses an index it does not have, which the media actor handles as a failed endpoint."""
REQUEST_TIMEOUT_S: Final = 15.0
"""How long one request waits for the brain's answer; the provider's own deadlines are shorter."""
_FORWARD_POLL_S: Final = 0.05
_FORWARD_BATCH: Final = 200
_PLAYBACK_ROWS_SQL: Final = (
    "SELECT id, event_uid, type, schema_version, ts_epoch_ms, payload_json, source_event_id, "
    "correlation_json FROM events WHERE id > ? AND type IN ({marks}) ORDER BY id LIMIT ?"
)


class RemoteTTSError(RuntimeError):
    """The brain could not run a provider call: unreachable, refused, or the provider failed."""


class BrainCallError(RuntimeError):
    """The brain did not answer a call of the capture session: unreachable, too slow, or refused."""


class Journal:
    """The scratch event log of a voice terminal: the answer's rows, and its playback rows.

    Emptied when constructed. Create it on the thread that appends to it (the link's loop);
    the media actor and the forwarder open their own connections to :attr:`path`.
    """

    def __init__(self, path: Path) -> None:
        """Delete any journal a previous run left at ``path`` and create an empty one."""
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        self.path = path
        self.conn = open_event_log(path)

    def append(self, frame: Mapping[str, Any]) -> bool:
        """Append one ``row`` frame from the brain, as it was; ``False`` if it is not one."""
        uid, kind, ts = frame.get("event_uid"), frame.get("event_type"), frame.get("ts_epoch_ms")
        payload, version = frame.get("payload"), frame.get("schema_version")
        if (
            not isinstance(uid, str) or HEX_ID.fullmatch(uid) is None
            or kind not in RESPONSE_ROW_TYPES or not isinstance(payload, dict)
            or type(ts) is not int or type(version) is not int
        ):
            return False
        try:
            emit_event(
                self.conn, type=kind, payload=payload, ts_epoch_ms=ts, schema_version=version,
                event_uid=uid, ingestion_node="brain",
            )
        except sqlite3.IntegrityError:
            return True  # the same row twice: a replay after a reconnect
        except EventLogError:
            LOGGER.warning("terminal voice: the brain sent a %s row the journal refuses", kind)
            return False
        if kind == "surface.response_open":
            # The join between a turn (heard here) and the answer's first audible sample, for
            # reading the end-of-speech latency from this terminal's own trace alone.
            record_realtime_trace(
                "terminal_response_open",
                turn_id=str(payload.get("turn_id", "")),
                response_id=str(payload.get("response_id", "")),
            )
        return True


class VoiceLink:
    """The terminal's side of the voice frames: one per process, across reconnects.

    ``on_frame`` runs on the link's loop; the provider sessions call everything else from the
    media actor's loop, so the state they share is behind a lock.
    """

    def __init__(self, journal: Journal) -> None:
        """Journal the brain's rows into ``journal``; no brain is connected yet."""
        self._journal = journal
        self._lock = threading.Lock()
        self._send: Callable[[str], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sessions: dict[str, RemoteTTSSession] = {}
        self._last_row: int | None = None
        self._closed = False
        self._brain_speaks = False
        self._asks: dict[str, concurrent.futures.Future[Any]] = {}

    @property
    def last_row(self) -> int | None:
        """The brain's id of the last answer row journaled, or ``None`` before any."""
        return self._last_row

    # -- the link's side ---------------------------------------------------------------

    def hello(self) -> dict[str, Any]:
        """What the hello frame adds: this terminal speaks, and where its journal stands."""
        return {"voice": True, "rows_after": self._last_row}

    def up(
        self, send: Callable[[str], Awaitable[None]], loop: asyncio.AbstractEventLoop,
        ready: Mapping[str, Any],
    ) -> None:
        """The brain said ``ready``; its frames can now be sent over ``send`` on ``loop``."""
        speaks = ready.get("voice") is True
        if not speaks:
            LOGGER.warning("the brain has no speech provider: this terminal will not be spoken to")
        if ready.get("listen") is not True:
            LOGGER.warning("the brain does not take a terminal's listening: no utterance of "
                           "this terminal will be answered")
        with self._lock:
            self._send, self._loop, self._brain_speaks = send, loop, speaks

    def down(self) -> None:
        """The link dropped: every provider session in flight fails, and none is resumed."""
        with self._lock:
            self._send, self._loop = None, None
            sessions, self._sessions = list(self._sessions.values()), {}
            asks, self._asks = list(self._asks.values()), {}
        for session in sessions:
            session.link_lost()
        for pending in asks:
            if not pending.done():
                pending.set_exception(BrainCallError("the brain link dropped"))

    def on_frame(self, frame: Mapping[str, Any]) -> None:
        """One ``row``, ``tts`` or ``reply`` frame from the brain."""
        if frame.get("type") == "reply":
            self._on_reply(frame)
            return
        if frame.get("type") == "row":
            row_id = frame.get("id")
            if type(row_id) is int and self._journal.append(frame):
                self._last_row = row_id
            return
        sid = frame.get("sid")
        with self._lock:
            session = self._sessions.get(sid) if isinstance(sid, str) else None
        if session is not None:
            session.receive(frame)

    def _on_reply(self, frame: Mapping[str, Any]) -> None:
        with self._lock:
            pending = self._asks.pop(str(frame.get("id")), None)
        if pending is None or pending.done():
            return
        if frame.get("ok") is True:
            pending.set_result(frame.get("value"))
        else:
            pending.set_exception(
                BrainCallError(f"{frame.get('code', 'refused')}: {frame.get('message', '')}"),
            )

    # -- the capture session's side ----------------------------------------------------

    def ask(self, op: str, args: Mapping[str, Any], *, timeout_s: float) -> Any:  # noqa: ANN401 — the brain's JSON.
        """Ask the brain to run ``op`` and wait for its answer; call from a worker thread.

        Raises:
            BrainCallError: no brain is connected, it did not answer within ``timeout_s``, the
                link dropped meanwhile, or it refused. The caller picks its own fallback.
        """
        try:
            on_link_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_link_loop = False
        if on_link_loop:
            msg = "a call to the brain must not run on the loop that carries the link"
            raise BrainCallError(msg)
        deadline = time.monotonic() + timeout_s
        rid = uuid.uuid4().hex
        answer: concurrent.futures.Future[Any] = concurrent.futures.Future()
        with self._lock:
            if self._send is None or self._closed:
                msg = f"the brain is not reachable, so {op} cannot be asked"
                raise BrainCallError(msg)
            self._asks[rid] = answer
        try:
            sent = self._submit({"type": "ask", "id": rid, "op": op, "args": dict(args)})
            sent.result(timeout=max(0.0, deadline - time.monotonic()))
            return answer.result(timeout=max(0.0, deadline - time.monotonic()))
        except BrainCallError:
            raise
        except TimeoutError:
            msg = f"the brain did not answer {op} within {timeout_s:g} s"
            raise BrainCallError(msg) from None
        except (RemoteTTSError, OSError, RuntimeError) as exc:
            msg = f"the brain link dropped while {op} was being asked"
            raise BrainCallError(msg) from exc
        finally:
            with self._lock:
                self._asks.pop(rid, None)

    def tell(self, op: str, args: Mapping[str, Any]) -> None:
        """Have the brain run ``op`` without waiting, in the order told.

        Dropped, and not retried, while the brain is unreachable.
        """
        self.post({"type": "ask", "op": op, "args": dict(args)})

    # -- the provider sessions' side ---------------------------------------------------

    def bind(self, session: RemoteTTSSession) -> None:
        """Register ``session`` for the brain's answers; it is refused while no brain is up."""
        with self._lock:
            if self._send is None or self._closed:
                msg = "the brain is not reachable, so there is no speech provider"
                raise RemoteTTSError(msg)
            if not self._brain_speaks:
                msg = "the brain has no speech provider"
                raise RemoteTTSError(msg)
            self._sessions[session.sid] = session

    def unbind(self, session: RemoteTTSSession) -> None:
        """``session`` is over."""
        with self._lock:
            self._sessions.pop(session.sid, None)

    def _submit(self, frame: Mapping[str, Any]) -> concurrent.futures.Future[None]:
        with self._lock:
            send, loop = self._send, self._loop
        if send is None or loop is None:
            msg = "the brain is not reachable, so there is no speech provider"
            raise RemoteTTSError(msg)
        text = json.dumps(frame, ensure_ascii=False)

        async def deliver() -> None:
            await send(text)

        return asyncio.run_coroutine_threadsafe(deliver(), loop)

    async def send(self, frame: Mapping[str, Any]) -> None:
        """Send ``frame`` on the link's loop, from the media actor's."""
        sent = self._submit(frame)
        try:
            await asyncio.wrap_future(sent)
        except asyncio.CancelledError:
            sent.cancel()
            raise
        except Exception as exc:
            msg = "the brain link dropped while a speech request was being sent"
            raise RemoteTTSError(msg) from exc

    def post(self, frame: Mapping[str, Any]) -> None:
        """Send ``frame`` without waiting: an abort must not hold up a barge-in."""
        with contextlib.suppress(RemoteTTSError):
            sent = self._submit(frame)
            sent.add_done_callback(lambda done: done.cancelled() or done.exception())

    def shutdown(self) -> None:
        """Stop for good: no new session binds, and the ones open fail."""
        self._closed = True
        self.down()


class RemoteTTSSession:
    """One provider session whose real half runs on the brain (``TTSSession``'s contract)."""

    def __init__(self, link: VoiceLink, sid: str, params: dict[str, Any]) -> None:
        """Create nothing yet: the brain creates its half at the first ``connect`` or ``open``."""
        self._link = link
        self.sid = sid
        self._params = params
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rid = 0
        self._pending: dict[int, asyncio.Future[None]] = {}
        # (kind, item): events from the brain, then one "end" or "error". Unbounded, but the
        # media actor sends the next segment only after the last one is played, so it holds
        # at most one segment of audio.
        self._events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._next_n = 0
        self._reader_claimed = False
        self._remote = False  # the brain has been told about this session
        self._closed = False
        self._ended = False
        self._lost: RemoteTTSError | None = None

    async def connect(self) -> None:
        """Connect the brain's provider session ahead of the answer."""
        await self._request("connect", params=self._params)

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        """Open the response session on the brain."""
        await self._request(
            "open", params=self._params, response_id=response_id,
            playback_generation_id=playback_generation_id,
        )

    async def send(self, segment: TTSResponseSegment) -> None:
        """Hand one segment to the brain's provider session."""
        await self._request(
            "send", response_id=segment.response_id,
            playback_generation_id=segment.playback_generation_id,
            sequence=segment.sequence, text=segment.text,
        )

    def audio_events(self) -> AsyncIterator[TTSAudioEvent]:
        """The brain's audio events for this session, in order; one reader."""
        if self._reader_claimed:
            msg = "TTSSession.audio_events() has exactly one reader"
            raise RuntimeError(msg)
        self._reader_claimed = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[TTSAudioEvent]:
        while True:
            kind, item = await self._events.get()
            if kind == "event":
                yield item
            elif kind == "end":
                return
            else:
                raise item

    async def finish(self) -> None:
        """A clean end: the brain finishes its session, then this one is over."""
        if self._closed:
            return
        try:
            await self._request("finish")
        finally:
            self._over()

    async def abort(self, reason: str) -> None:
        """Stop at once: the brain is told without waiting, and this session is over."""
        self._teardown("abort", reason=reason)

    async def close(self) -> None:
        """Close the session on both sides; a brain that cannot be told has dropped it already."""
        self._teardown("close")

    # -- both loops ----------------------------------------------------------------------

    async def _request(self, op: str, **fields: Any) -> None:  # noqa: ANN401 — frame fields.
        if self._lost is not None:
            raise self._lost
        if self._closed:
            msg = "the speech session is closed"
            raise TTSSessionClosedError(msg)
        loop = self._loop = asyncio.get_running_loop()
        self._link.bind(self)
        self._remote = True
        self._rid += 1
        rid = self._rid
        answered: asyncio.Future[None] = loop.create_future()
        self._pending[rid] = answered
        try:
            await self._link.send(
                {"type": "tts", "sid": self.sid, "rid": rid, "op": op, **fields},
            )
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                await answered
        except TimeoutError:
            msg = f"the brain did not answer the speech request {op!r}"
            raise RemoteTTSError(msg) from None
        finally:
            self._pending.pop(rid, None)

    def _teardown(self, op: str, **fields: Any) -> None:  # noqa: ANN401 — frame fields.
        if self._closed:
            return
        self._closed = True
        if self._remote and self._lost is None:
            self._link.post({"type": "tts", "sid": self.sid, "op": op, **fields})
        self._over()

    def _over(self) -> None:
        """Release the reader and anything waiting; runs on the media actor's loop."""
        self._closed = True
        self._link.unbind(self)
        self._end(("end", None))
        gone = TTSSessionClosedError("the speech session closed")
        for answered in list(self._pending.values()):
            if not answered.done():
                answered.set_exception(gone)

    def _end(self, last: tuple[str, Any]) -> None:
        if not self._ended:
            self._ended = True
            self._events.put_nowait(last)

    # -- the link's loop -----------------------------------------------------------------

    def receive(self, frame: Mapping[str, Any]) -> None:
        """A frame for this session, from the link's loop."""
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # the media loop has stopped
                loop.call_soon_threadsafe(self._handle, dict(frame))

    def link_lost(self) -> None:
        """The brain link dropped under this session."""
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._fail, "the brain link dropped")

    def _fail(self, why: str) -> None:
        self._lost = RemoteTTSError(why)
        self._end(("error", self._lost))
        for answered in list(self._pending.values()):
            if not answered.done():
                answered.set_exception(self._lost)

    def _handle(self, frame: dict[str, Any]) -> None:
        """Runs on the media actor's loop."""
        if self._closed:
            return  # late audio for a session already aborted
        op = frame.get("op")
        if "rid" in frame and "ok" in frame:
            answered = self._pending.get(frame["rid"]) if type(frame["rid"]) is int else None
            if answered is None or answered.done():
                return
            if frame["ok"] is True:
                answered.set_result(None)
            else:
                answered.set_exception(RemoteTTSError(_describe(frame.get("error"))))
        elif op == "audio":
            try:
                if frame.get("n") != self._next_n:
                    msg = "audio events arrived out of order"
                    raise ValueError(msg)  # noqa: TRY301 — one failure path below.
                event = decode_event(frame.get("event"))
            except ValueError as exc:
                self._fail(f"the brain sent audio this terminal cannot use ({exc})")
                return
            self._next_n += 1
            self._events.put_nowait(("event", event))
        elif op == "end":
            self._end(("end", None))
        elif op == "failed":
            self._end(("error", RemoteTTSError(_describe(frame.get("error")))))


def _describe(error: object) -> str:
    """A brain's failure body as one sentence."""
    if isinstance(error, dict):
        return f"{error.get('name', 'Error')}: {error.get('message', '')}"
    return "the brain's speech provider failed"


class RemoteTTSProvider:
    """The media actor's ``StreamingTTSProvider``, with the brain's provider behind it."""

    def __init__(self, link: VoiceLink) -> None:
        """Make sessions that talk over ``link``."""
        self._link = link

    @property
    def streaming_candidate_count(self) -> int:
        """The endpoints a session may name, as the real provider has them."""
        return REMOTE_ENDPOINTS

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        language: Language,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> RemoteTTSSession:
        """A session that is not yet known to the brain; the first call that needs it says so."""
        return RemoteTTSSession(
            self._link,
            uuid.uuid4().hex,
            {
                "endpoint_index": endpoint_index,
                "language": language,
                "idle_close_s": idle_close_s,
                "command_queue_capacity": command_queue_capacity,
                "audio_queue_capacity": audio_queue_capacity,
            },
        )

    def request_close(self) -> None:
        """The media actor is stopping: no session survives it."""
        self._link.shutdown()


async def forward_playback(
    journal_path: Path, outbox: EventOutbox, *, interval_s: float = _FORWARD_POLL_S,
) -> None:
    """Send every playback row the media actor writes into the journal to the brain.

    Each goes under its own event id, so the brain's own copy is the one the heard prefix and
    the captions fold from, and a resend after a reconnect is recognised. Runs until cancelled.
    """
    marks = ",".join("?" for _ in PLAYBACK_EVENT_TYPES)
    sql = _PLAYBACK_ROWS_SQL.format(marks=marks)
    conn = open_runtime_event_log(journal_path)
    after = 0
    try:
        while True:
            try:
                rows = conn.execute(sql, (after, *sorted(PLAYBACK_EVENT_TYPES), _FORWARD_BATCH))
                found = rows.fetchall()
            except sqlite3.Error:
                LOGGER.exception("terminal voice: reading the journal failed; retrying")
                await asyncio.sleep(1.0)
                continue
            for row_id, uid, kind, version, ts, payload, source, correlation in found:
                after = row_id
                event = Event(
                    uid, kind, version, ts, json.loads(payload), source,
                    None if correlation is None else json.loads(correlation),
                )
                try:
                    outbox.forward(event)
                except ValueError:
                    LOGGER.exception("terminal voice: a %s row cannot be sent to the brain", kind)
            if len(found) < _FORWARD_BATCH:
                await asyncio.sleep(interval_s)
    finally:
        conn.close()
