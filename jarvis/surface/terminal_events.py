"""ADR 0170: the observers' events a terminal pushes into the brain's log.

An observer on a terminal reads this machine's files and apps exactly as it does on one
machine, but the events it produces go to the brain's event log, tagged with the terminal's
device name. Two more frames ride the ``/terminal/ws`` link of :mod:`jarvis.surface.terminal_link`::

    terminal -> brain   {"type": "event", "event_uid": "<32 hex>", "event_type": "...",
                         "ts_epoch_ms": 1700000000000, "payload": {...}}
    brain -> terminal   {"type": "ack", "event_uid": "<32 hex>", "ok": true}
    brain -> terminal   {"type": "ack", "event_uid": "<32 hex>", "ok": false, "code": "..."}

A frame may also carry ``"schema_version"``, ``"source_event_id"`` and ``"correlation"``: the
playback rows of a voice terminal (ADR 0172) keep their own ids and the links between them.

and the brain's ``ready`` frame carries ``"baseline"``: the latest ``repo.state_observed`` per
repo, the latest ``timesink.state_observed`` and the latest ``usage.state_observed`` per
service, which is what the observers fold their
change-baselines from (ADR-0009 D5). The brain is where that state lives, so a terminal
that restarts picks up from what the brain last heard.

This module holds both ends: :class:`BrainEvents` (validate, dedupe and append on the brain)
and :class:`EventOutbox` (what an observer on a terminal emits into, and what the link sends).
:class:`BrainEvents` takes the set of types it accepts as a parameter: a terminal's link passes
the observers' (and, for a voice terminal, the playback rows), a phone's batch over HTTP
(ADR 0197, :mod:`jarvis.surface.phone_events`) passes the phone's own, and neither accepts the
other's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import time
import uuid
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared import Event
from jarvis.state.event_log import EventLogError, EventTypeRegistry, emit_event
from jarvis.surface import phone_events, repo_observer, timesink_observer, usage_observer

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

LOGGER = logging.getLogger("jarvis.surface.terminal_events")

OBSERVER_EVENT_TYPES: Final = frozenset(
    {
        repo_observer.STATE_EVENT_TYPE,
        repo_observer.COMMIT_EVENT_TYPE,
        timesink_observer.EVENT_TYPE,
        usage_observer.EVENT_TYPE,
    },
)
"""Every type a terminal may write: what the observers that run there produce, and nothing
else. No confirmation, no utterance, no tool result."""

PLAYBACK_EVENT_TYPES: Final = frozenset(
    {
        "surface.playback_started",
        "surface.playback_segment_prepared",
        "surface.playback_alignment",
        "surface.playback_checkpoint",
        "surface.playback_completed",
        "surface.playback_interrupted",
        "surface.playback_failed",
        "surface.playback_lane_isolated",
        "surface.speech_dropped",
        "tts.usage_observed",
    },
)
"""What the media actor of a voice terminal (ADR 0172) writes, and the brain folds: the heard
prefix and the captions, and the provider's character count. Only a terminal that declared
voice may send them, each under the id the media actor gave it."""
VOICE_TERMINAL_EVENT_TYPES: Final = OBSERVER_EVENT_TYPES | PLAYBACK_EVENT_TYPES
"""What a terminal that declared voice may write: the observers' events and the playback rows."""

UTTERANCE_CHANNEL: Final = "inherent_wake"
"""The one channel a terminal's capture session commits an utterance on."""
_MAX_TRANSCRIPT_CHARS: Final = 20_000
_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_UTTERANCE_TEXT_FIELDS: Final = {
    "language": 32, "language_detected": 64, "emotion": 64, "audio_artifact_ref": 1024,
    "session_id": 64, "endpoint_reason": 64,
}
_UTTERANCE_UID_NAMESPACE: Final = uuid.UUID("5f0c6a4e-0b1d-4f6e-9a57-6a1b2f0c1d00")

MAX_EVENT_CHARS: Final = 64 * 1024
"""One event frame's cap; the biggest observer payload is a few hundred bytes."""
MAX_PENDING: Final = 1000
"""Events a terminal keeps for a brain it cannot reach; the oldest go first."""
_MAX_BASELINE_ROWS: Final = 200
_EVENT_UID: Final = re.compile(r"[0-9a-f]{32}")
_PAST_S: Final = 7 * 24 * 3600
_FUTURE_S: Final = 300

_LATEST_REPO_STATES_SQL: Final = (
    "SELECT type, payload_json FROM events WHERE id IN ("
    "SELECT MAX(id) FROM events WHERE type = ? GROUP BY json_extract(payload_json, '$.repo_path')"
    ") ORDER BY id LIMIT ?"
)
_LATEST_USAGE_SQL: Final = (
    "SELECT type, payload_json FROM events WHERE id IN ("
    "SELECT MAX(id) FROM events WHERE type = ? GROUP BY json_extract(payload_json, '$.service')"
    ") ORDER BY id LIMIT ?"
)
_LATEST_TYPE_SQL: Final = (
    "SELECT type, payload_json FROM events WHERE type = ? ORDER BY id DESC LIMIT 1"
)


class BrainEvents:
    """The brain's end: append what an allowed observer sent, once, under the device's name.

    Runs on the loop thread that owns ``conn`` (the route is async, like every other reader
    of the runtime's log).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        """Append to ``conn``, the runtime's own event log."""
        self._conn = conn

    def baseline(self) -> list[dict[str, Any]]:
        """The rows an observer folds its baseline from: ``[{"event_type", "payload"}, ...]``."""
        rows = self._conn.execute(
            _LATEST_REPO_STATES_SQL, (repo_observer.STATE_EVENT_TYPE, _MAX_BASELINE_ROWS),
        ).fetchall()
        rows += self._conn.execute(_LATEST_TYPE_SQL, (timesink_observer.EVENT_TYPE,)).fetchall()
        rows += self._conn.execute(
            _LATEST_USAGE_SQL, (usage_observer.EVENT_TYPE, len(usage_observer.SERVICES)),
        ).fetchall()
        return [{"event_type": kind, "payload": json.loads(payload)} for kind, payload in rows]

    def record_utterance(self, device: str, args: Mapping[str, Any]) -> str:
        """Append the ``utterance.received`` a voice terminal heard, once; its event uid.

        The uid is derived from ``device`` and the utterance's own id, so a terminal that
        sends the same utterance again (it never saw the answer) gets the first one's uid and
        nothing is written twice. The payload is rebuilt from the fields the voice pipeline
        writes; nothing else a terminal sends is kept. ``audio_artifact_ref`` is a path on
        the terminal's own disk: the audio itself never leaves it.

        Raises:
            ValueError: a field is missing or has the wrong shape.
            sqlite3.Error: the log could not be written; the utterance is not recorded.
        """
        transcript, turn_id, utterance_id = (
            args.get("transcript"), args.get("turn_id"), args.get("utterance_id"),
        )
        confidence = args.get("confidence")
        if (
            not isinstance(transcript, str) or not 0 < len(transcript) <= _MAX_TRANSCRIPT_CHARS
            or not isinstance(turn_id, str) or _ID.fullmatch(turn_id) is None
            or not isinstance(utterance_id, str) or _ID.fullmatch(utterance_id) is None
            or args.get("channel") != UTTERANCE_CHANNEL
            or not (
                confidence is None
                or (isinstance(confidence, int | float) and not isinstance(confidence, bool))
            )
        ):
            msg = "not an utterance this brain accepts"
            raise ValueError(msg)
        payload: dict[str, Any] = {
            "transcript": transcript, "turn_id": turn_id, "channel": UTTERANCE_CHANNEL,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        for key, limit in _UTTERANCE_TEXT_FIELDS.items():
            value = args.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > limit:
                msg = f"utterance field {key} is not text of at most {limit} characters"
                raise ValueError(msg)
            payload[key] = value
        payload["utterance_id"] = utterance_id
        uid = uuid.uuid5(_UTTERANCE_UID_NAMESPACE, f"{device}\0{utterance_id}").hex
        if self._conn.execute("SELECT 1 FROM events WHERE event_uid = ?", (uid,)).fetchone():
            return uid
        try:
            emit_event(
                self._conn, type="utterance.received", payload=payload,
                correlation={"turn_id": turn_id}, event_uid=uid, ingestion_node=device,
            )
        except EventLogError as exc:
            raise ValueError(str(exc)) from exc
        return uid

    def record(
        self, device: str, frame: Mapping[str, Any], size: int, allowed: Collection[str],
    ) -> dict[str, Any] | None:
        """Validate and append one ``event`` frame from ``device``; the ``ack`` to send back.

        ``allowed`` is the set of event types this sender may write: a type outside it is
        refused. A phone's types are also held to the strict payload check of
        :func:`jarvis.surface.phone_events.payload_valid`.

        A refused event is acknowledged with its code and never retried; a duplicate (a
        sender resending what it was not told arrived) is acknowledged as done. A database
        error gets no answer (``None``), so the sender keeps the event and sends it again.
        """
        uid = frame.get("event_uid")
        if not isinstance(uid, str) or _EVENT_UID.fullmatch(uid) is None:
            return _ack(None, "bad_event")
        now_ms = int(time.time() * 1000)
        code = _refusal(frame, size, allowed, now_ms)
        if code is not None:
            return _ack(uid, code)
        event_type, payload, ts = frame["event_type"], frame["payload"], frame["ts_epoch_ms"]
        if not now_ms - _PAST_S * 1000 <= ts <= now_ms + _FUTURE_S * 1000:
            ts = now_ms  # a clock that far off is not trusted to place the event in the day
        try:
            if self._conn.execute("SELECT 1 FROM events WHERE event_uid = ?", (uid,)).fetchone():
                return _ack(uid, None)
            emit_event(
                self._conn,
                type=event_type,
                payload=payload,
                ts_epoch_ms=ts,
                event_uid=uid,
                ingestion_node=device,
                schema_version=frame.get("schema_version"),
                source_event_id=frame.get("source_event_id"),
                correlation=frame.get("correlation"),
            )
        except EventLogError:
            return _ack(uid, "bad_event")
        except sqlite3.Error:
            LOGGER.exception("terminal %s: appending %s failed", device, event_type)
            return None
        return _ack(uid, None)

    def record_phone_batch(self, device: str, frames: Sequence[object]) -> list[dict[str, Any]]:
        """Append a phone's batch of frames (ADR 0197); one ``ack`` per frame, in order.

        Each frame goes through :meth:`record` with only the phone's types allowed, so it is
        deduplicated by its ``event_uid``, has its time clamped and is appended under
        ``device`` exactly as a terminal's event is. A frame that is not an object, or carries
        a key an event frame does not, is ``bad_event``. A database error is ``retry`` for that
        frame alone, the one code the phone answers by keeping the frame and sending it again.
        """
        acks: list[dict[str, Any]] = []
        for frame in frames:
            if not isinstance(frame, dict) or not phone_events.frame_shaped(frame):
                uid = frame.get("event_uid") if isinstance(frame, dict) else None
                valid = isinstance(uid, str) and _EVENT_UID.fullmatch(uid) is not None
                acks.append(_ack(uid if valid else None, "bad_event"))
                continue
            size = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
            ack = self.record(device, frame, size, phone_events.PHONE_EVENT_TYPES)
            acks.append(_ack(frame["event_uid"], "retry") if ack is None else ack)
        return acks


def _well_formed(frame: Mapping[str, Any]) -> bool:
    """Whether an event frame's fields have the shapes ``emit_event`` is given."""
    ts, version = frame.get("ts_epoch_ms"), frame.get("schema_version")
    source, correlation = frame.get("source_event_id"), frame.get("correlation")
    return (
        isinstance(frame.get("payload"), dict)
        and type(ts) is int
        and (version is None or type(version) is int)
        and (source is None or isinstance(source, str))
        and (
            correlation is None
            or (
                isinstance(correlation, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in correlation.items())
            )
        )
    )


def _refusal(
    frame: Mapping[str, Any], size: int, allowed: Collection[str], now_ms: int,
) -> str | None:
    """Why an event frame is refused for good, or ``None``.

    A phone's event must also carry exactly the payload its type defines, judged against the
    brain's clock ``now_ms`` (:func:`jarvis.surface.phone_events.payload_valid`).
    """
    if size > MAX_EVENT_CHARS:
        return "event_too_large"
    kind = frame.get("event_type")
    if not isinstance(kind, str) or kind not in allowed:
        return "event_type_not_allowed"
    if not _well_formed(frame):
        return "bad_event"
    if kind in phone_events.PHONE_EVENT_TYPES and not phone_events.payload_valid(
        kind, frame["payload"], now_ms,
    ):
        return "bad_event"
    return None


def _ack(uid: str | None, code: str | None) -> dict[str, Any]:
    if code is None:
        return {"type": "ack", "event_uid": uid, "ok": True}
    return {"type": "ack", "event_uid": uid, "ok": False, "code": code}


class EventOutbox:
    """The terminal's end: the observers' ``emit_event``, and the link's queue of what to send.

    Create it inside the running loop and call :meth:`emit_event` only from that loop's
    thread, as the observers' emit halves do. Events wait here until the brain acknowledges
    them. While the brain is unreachable at most :data:`MAX_PENDING` are kept, the oldest
    dropped first, and nothing survives the process: the brain's baseline is the last event
    it received, so a restarted terminal observes again from there.
    """

    def __init__(self, *, max_pending: int = MAX_PENDING) -> None:
        """Start empty, with no baseline yet."""
        self._max_pending = max_pending
        self._pending: dict[str, str] = {}  # event_uid -> frame text, oldest first
        self._wake = asyncio.Event()
        self._baseline: list[dict[str, Any]] | None = None
        self._has_baseline = asyncio.Event()
        self.dropped = 0

    def emit_event(
        self,
        conn: sqlite3.Connection,  # noqa: ARG002 — the observers' call shape: `emit_event(log, ...)`.
        *,
        type: str,  # noqa: A002 — matches `emit_event`'s own name for it.
        payload: Mapping[str, Any],
    ) -> Event:
        """Queue one event as the observer would have appended it.

        Raises:
            ValueError: the type is not one an observer produces, or the frame is too big.
        """
        schema = EventTypeRegistry.get(type)
        if type not in OBSERVER_EVENT_TYPES or schema is None:
            msg = f"{type!r} is not an event a terminal may send"
            raise ValueError(msg)
        uid = uuid.uuid4().hex
        ts = int(time.time() * 1000)
        text = json.dumps(
            {
                "type": "event",
                "event_uid": uid,
                "event_type": type,
                "ts_epoch_ms": ts,
                "payload": dict(payload),
            },
            ensure_ascii=False,
        )
        if len(text) > MAX_EVENT_CHARS:
            msg = f"a {type} event of {len(text)} characters is over the {MAX_EVENT_CHARS} cap"
            raise ValueError(msg)
        self._queue(uid, text)
        return Event(uid, type, schema.schema_version, ts, dict(payload), None, None)

    def _queue(self, uid: str, text: str) -> None:
        """Hold ``text`` until the brain acknowledges ``uid``, dropping the oldest when full."""
        if len(self._pending) >= self._max_pending:
            del self._pending[next(iter(self._pending))]
            self.dropped += 1
            LOGGER.warning("the brain is unreachable: dropped the oldest event (%d so far)",
                           self.dropped)
        self._pending[uid] = text
        self._wake.set()

    def forward(self, event: Event) -> None:
        """Queue a playback row the media actor wrote, under its own id (ADR 0172).

        Raises:
            ValueError: the type is not a playback row, or the frame is too big.
        """
        if event.type not in PLAYBACK_EVENT_TYPES:
            msg = f"{event.type!r} is not a row a voice terminal may send"
            raise ValueError(msg)
        text = json.dumps(
            {
                "type": "event",
                "event_uid": event.event_uid,
                "event_type": event.type,
                "schema_version": event.schema_version,
                "ts_epoch_ms": event.ts_epoch_ms,
                "payload": dict(event.payload),
                "source_event_id": event.source_event_id,
                "correlation": None if event.correlation is None else dict(event.correlation),
            },
            ensure_ascii=False,
        )
        if len(text) > MAX_EVENT_CHARS:
            msg = f"a {event.type} row of {len(text)} characters is over the {MAX_EVENT_CHARS} cap"
            raise ValueError(msg)
        self._queue(event.event_uid, text)

    def ready(self, frame: Mapping[str, Any]) -> None:
        """The brain's ``ready``: its baseline rows, taken once; later connects keep their own."""
        rows = frame.get("baseline")
        if self._baseline is None:
            self._baseline = [
                row for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict) and isinstance(row.get("payload"), dict)
            ]
            self._has_baseline.set()

    async def baseline(self) -> list[dict[str, Any]]:
        """Wait for the first connection to the brain, then return its baseline rows."""
        await self._has_baseline.wait()
        return self._baseline or []

    def ack(self, frame: Mapping[str, Any]) -> None:
        """The brain's ``ack``: the event is in its log, or refused for good; either way done."""
        uid = frame.get("event_uid")
        if not isinstance(uid, str):
            return
        if frame.get("ok") is not True and uid in self._pending:
            LOGGER.warning("the brain refused an event (%s); dropped", frame.get("code"))
        self._pending.pop(uid, None)

    async def pump(self, ws: Any) -> None:  # noqa: ANN401 — a websockets connection.
        """Send everything not yet acknowledged, then each new event, until the socket fails."""
        sent: set[str] = set()  # per connection: a reconnect sends the unacknowledged again
        self._wake.set()
        while True:
            await self._wake.wait()
            self._wake.clear()
            sent.intersection_update(self._pending)
            for uid, text in list(self._pending.items()):
                if uid not in sent:
                    await ws.send(text)
                    sent.add(uid)
