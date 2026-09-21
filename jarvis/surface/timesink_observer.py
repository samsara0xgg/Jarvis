"""TimeSink head observer (ADR 0023) — a cheap "is there new data" poll, never a model call.

An L5 observer shaped like the usage observer: one poll reads the store's
head (max row ids, the latest span end, the latest capture ``lastSeenAt``
and the latest state event) inside one read transaction, and
``timesink.state_observed`` is emitted only when that head changed. The
baseline is recovered from the event log, so a restart or a wake emits at
most one row for everything that happened meanwhile. Rows that keep
extending (``end`` / ``lastSeenAt`` moving) change the head without a new
id, which is why the head carries those instants and not ids alone.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from jarvis.state import timesink
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.shared import Event

EVENT_TYPE = "timesink.state_observed"
OBSERVER_ACTOR = "observer"
_SELECT_LATEST_SQL = (
    "SELECT payload_json, ts_epoch_ms FROM events WHERE type = ? ORDER BY id DESC LIMIT 1"
)


@dataclass(frozen=True)
class TimesinkHead:
    """What the store's tail looked like at one instant; equality is the emit-on-change test."""

    status: str
    identity: str | None
    span_high: int
    span_latest_end: str | None
    capture_high: int
    capture_latest_seen: str | None
    state_high: int
    state_latest: dict[str, str] | None
    reason: str | None = None


def _unavailable(reason: str) -> TimesinkHead:
    return TimesinkHead("unavailable", None, 0, None, 0, None, 0, None, reason)


def collect(path: Path | None) -> TimesinkHead:
    """Read the head inside one read transaction; unreadable stores are a status, not a raise."""
    if path is None:
        return _unavailable("observer.timesink is disabled")
    with timesink.snapshot(path) as snap:
        if snap is None:
            return _unavailable("TimeSink database is missing, unreadable or incompatible")
        try:
            span = snap.conn.execute("SELECT COALESCE(MAX(id),0), MAX(end) FROM span").fetchone()
            capture = snap.conn.execute(
                "SELECT COALESCE(MAX(id),0), MAX(lastSeenAt) FROM capture"
            ).fetchone()
            state = snap.conn.execute(
                "SELECT id, at, kind FROM stateEvent ORDER BY id DESC LIMIT 1"
            ).fetchone()
        except sqlite3.Error as exc:
            return _unavailable(f"TimeSink schema is incompatible: {type(exc).__name__}")
        try:
            return TimesinkHead(
                status="ok",
                identity=snap.identity,
                span_high=int(span[0]),
                span_latest_end=timesink.moment(span[1]) if span[1] else None,
                capture_high=int(capture[0]),
                capture_latest_seen=timesink.moment(capture[1]) if capture[1] else None,
                state_high=int(state["id"]) if state else 0,
                state_latest=(
                    {"at": timesink.moment(state["at"]), "kind": str(state["kind"])}
                    if state
                    else None
                ),
            )
        except (ValueError, TypeError) as exc:
            return _unavailable(f"TimeSink contains invalid data: {type(exc).__name__}")


def _head_from_payload(payload: Mapping[str, Any]) -> TimesinkHead | None:
    try:
        return TimesinkHead(
            status=str(payload["status"]),
            identity=payload["identity"],
            span_high=int(payload["span_high"]),
            span_latest_end=payload["span_latest_end"],
            capture_high=int(payload["capture_high"]),
            capture_latest_seen=payload["capture_latest_seen"],
            state_high=int(payload["state_high"]),
            state_latest=payload["state_latest"],
            reason=payload.get("reason"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def latest_observation(event_log: sqlite3.Connection) -> dict[str, Any] | None:
    """Read model: the last observed head plus when it was observed (dashboard "data time")."""
    row = event_log.execute(_SELECT_LATEST_SQL, (EVENT_TYPE,)).fetchone()
    if row is None:
        return None
    payload = json.loads(row[0])
    payload.setdefault("observed_at_ms", row[1])
    return dict(payload)


class TimesinkObserver:
    """Emit-on-change head perception over a log-recovered baseline."""

    def __init__(self, event_log: sqlite3.Connection, path: Path | None) -> None:
        """Bind the observer to the log connection and the configured store path."""
        self._event_log = event_log
        self._path = path
        self._baseline: TimesinkHead | None = None

    def recover_baseline(self) -> TimesinkHead | None:
        """Seed the baseline from the latest emitted row; call at startup on the loop thread."""
        row = self._event_log.execute(_SELECT_LATEST_SQL, (EVENT_TYPE,)).fetchone()
        self._baseline = _head_from_payload(json.loads(row[0])) if row else None
        return self._baseline

    def collect(self) -> TimesinkHead:
        """The ``asyncio.to_thread`` half: file I/O only."""
        return collect(self._path)

    def emit(self, head: TimesinkHead) -> Event | None:
        """Append one row when the head differs from the baseline; on the connection's thread."""
        if head == self._baseline:
            return None
        event = emit_event(
            self._event_log,
            type="timesink.state_observed",  # literal: the observer canary scans it.
            payload={
                **asdict(head),
                "observed_at_ms": int(time.time() * 1000),
                "actor": OBSERVER_ACTOR,
            },
        )
        self._baseline = head
        return event
