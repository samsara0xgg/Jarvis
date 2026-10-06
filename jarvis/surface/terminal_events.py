"""ADR 0170: the observers' events a terminal pushes into the brain's log.

An observer on a terminal reads this machine's files and apps exactly as it does on one
machine, but the events it produces go to the brain's event log, tagged with the terminal's
device name. Two more frames ride the ``/terminal/ws`` link of :mod:`jarvis.surface.terminal_link`::

    terminal -> brain   {"type": "event", "event_uid": "<32 hex>", "event_type": "...",
                         "ts_epoch_ms": 1700000000000, "payload": {...}}
    brain -> terminal   {"type": "ack", "event_uid": "<32 hex>", "ok": true}
    brain -> terminal   {"type": "ack", "event_uid": "<32 hex>", "ok": false, "code": "..."}

and the brain's ``ready`` frame carries ``"baseline"``: the latest ``repo.state_observed`` per
repo and the latest ``timesink.state_observed``, which is what the observers fold their
change-baselines from (ADR-0009 D5). The brain is where that state lives, so a terminal
that restarts picks up from what the brain last heard.

This module holds both ends: :class:`BrainEvents` (validate, dedupe and append on the brain)
and :class:`EventOutbox` (what an observer on a terminal emits into, and what the link sends).
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
from jarvis.surface import repo_observer, timesink_observer

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger("jarvis.surface.terminal_events")

OBSERVER_EVENT_TYPES: Final = frozenset(
    {
        repo_observer.STATE_EVENT_TYPE,
        repo_observer.COMMIT_EVENT_TYPE,
        timesink_observer.EVENT_TYPE,
    },
)
"""Every type a terminal may write: what the observers that run there produce, and nothing
else. No confirmation, no utterance, no tool result."""

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
        return [{"event_type": kind, "payload": json.loads(payload)} for kind, payload in rows]

    def record(self, device: str, frame: Mapping[str, Any], size: int) -> dict[str, Any] | None:
        """Validate and append one ``event`` frame from ``device``; the ``ack`` to send back.

        A refused event is acknowledged with its code and never retried; a duplicate (a
        terminal resending what it was not told arrived) is acknowledged as done. A database
        error gets no answer (``None``), so the terminal keeps the event and sends it again
        the next time it connects.
        """
        uid = frame.get("event_uid")
        if not isinstance(uid, str) or _EVENT_UID.fullmatch(uid) is None:
            return _ack(None, "bad_event")
        code = _refusal(frame, size)
        if code is not None:
            return _ack(uid, code)
        event_type, payload, ts = frame["event_type"], frame["payload"], frame["ts_epoch_ms"]
        now_ms = int(time.time() * 1000)
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
            )
        except EventLogError:
            return _ack(uid, "bad_event")
        except sqlite3.Error:
            LOGGER.exception("terminal %s: appending %s failed", device, event_type)
            return None
        return _ack(uid, None)


def _refusal(frame: Mapping[str, Any], size: int) -> str | None:
    """Why an event frame is refused for good, or ``None``."""
    ts = frame.get("ts_epoch_ms")
    if size > MAX_EVENT_CHARS:
        return "event_too_large"
    if frame.get("event_type") not in OBSERVER_EVENT_TYPES:
        return "event_type_not_allowed"
    if not isinstance(frame.get("payload"), dict):
        return "bad_event"
    if isinstance(ts, bool) or not isinstance(ts, int):
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
        if len(self._pending) >= self._max_pending:
            del self._pending[next(iter(self._pending))]
            self.dropped += 1
            LOGGER.warning("the brain is unreachable: dropped the oldest event (%d so far)",
                           self.dropped)
        self._pending[uid] = text
        self._wake.set()
        return Event(uid, type, schema.schema_version, ts, dict(payload), None, None)

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
