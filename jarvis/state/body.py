"""ADR 0388: his body as Health recorded it, asked by metric and stretch of time.

The phone sends every Health sample as Health recorded it (ADR 0293): readings as
``phone.measure_observed`` and spans and hourly totals as ``phone.health_observed``; the watch's
charge comes through the phone as ``phone.state_observed`` (ADR 0377). These are the named
questions the ``body`` tool asks of them. Every row's ``ts_epoch_ms`` is the end of the span it
covers, so a stretch is read on the ``(type, ts_epoch_ms)`` index and kept by the sample's own
start.

Layer rules: stdlib, :mod:`jarvis.shared` and other :mod:`jarvis.state` modules only (L2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import EVENT_COLUMNS, row_to_event
from jarvis.state.phone_day import HEALTH_EVENT, MEASURE_EVENT

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from jarvis.shared import Event

STATE_EVENT: Final = "phone.state_observed"
WATCH_BATTERY: Final = "watch_battery"
_HOUR_MS: Final = 60 * 60_000
SPAN_REACH_MS: Final = 24 * _HOUR_MS
"""How long a span sample may last: a stage, a workout or a total is read up to this far past the
end of the stretch, since its row is timed at its end."""


@dataclass(frozen=True)
class Reading:
    """One reading Health holds: when it was taken, its value in the unit the phone sent."""

    at_ms: int
    value: float
    at_rest: bool | None
    """Health's motion context: True sedentary, False active, None when the source set none."""
    source: str


@dataclass(frozen=True)
class Total:
    """One hourly total of a cumulative metric, the latest the phone sent for that hour."""

    start_ms: int
    end_ms: int
    value: float


@dataclass(frozen=True)
class Charge:
    """One reading of the watch's charge: its percent and charging, full or unplugged."""

    at_ms: int
    percent: int
    state: str


def _payloads(
    conn: sqlite3.Connection, event_type: str, metrics: Iterable[str], since_ms: int,
    until_ms: int,
) -> list[dict[str, Any]]:
    """The payloads of these metrics of one phone type logged in ``[since_ms, until_ms)``.

    In the order they arrived; one pass however many metrics are asked for.
    """
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE type = ? AND ts_epoch_ms >= ? AND ts_epoch_ms < ?"
        " AND json_extract(payload_json, '$.metric') IN (SELECT value FROM json_each(?))"
        " ORDER BY id",
        (event_type, since_ms, until_ms, json.dumps(sorted(set(metrics)))),
    )
    return [json.loads(raw) for (raw,) in rows]


def readings(
    conn: sqlite3.Connection, metrics: Iterable[str], start_ms: int, end_ms: int,
) -> dict[str, list[Reading]]:
    """Every reading of these measured metrics that starts in ``[start_ms, end_ms)``, once each.

    By metric and then by time. A sample sent twice (the same ``sample_uuid``, as after a
    reinstall resets the phone's anchor) counts once.
    """
    seen: set[str] = set()
    found: dict[str, list[Reading]] = {metric: [] for metric in metrics}
    if not found:
        return found
    for row in _payloads(conn, MEASURE_EVENT, found, start_ms, end_ms + SPAN_REACH_MS):
        start = int(row["start_ms"])
        uuid = row.get("sample_uuid")
        if not start_ms <= start < end_ms or (isinstance(uuid, str) and uuid.lower() in seen):
            continue
        if isinstance(uuid, str):
            seen.add(uuid.lower())
        meta = row.get("meta")
        context = meta.get("motion_context") if isinstance(meta, dict) else None
        found[str(row["metric"])].append(Reading(
            start, float(row["value"]),
            {"sedentary": True, "active": False}.get(str(context)),
            str(row.get("source") or ""),
        ))
    return {metric: sorted(held, key=lambda one: one.at_ms) for metric, held in found.items()}


def totals(
    conn: sqlite3.Connection, metrics: Iterable[str], start_ms: int, end_ms: int,
) -> dict[str, list[Total]]:
    """The hourly totals of these cumulative metrics whose hour starts in ``[start_ms, end_ms)``.

    By metric and then by hour. A total sent again for the same hour (the watch delivered late)
    is the later one.
    """
    latest: dict[str, dict[tuple[int, int], float]] = {metric: {} for metric in metrics}
    if not latest:
        return {}
    for row in _payloads(conn, HEALTH_EVENT, latest, start_ms, end_ms + SPAN_REACH_MS):
        span = (int(row["start_ms"]), int(row["end_ms"]))
        if start_ms <= span[0] < end_ms:
            latest[str(row["metric"])][span] = float(row["value"])
    return {
        metric: [Total(start, end, value) for (start, end), value in sorted(held.items())]
        for metric, held in latest.items()
    }


def span_events(
    conn: sqlite3.Connection, metrics: Iterable[str], start_ms: int, end_ms: int,
) -> list[Event]:
    """The span samples of these metrics that overlap ``[start_ms, end_ms)``, as logged.

    Sleep stages, workouts, mindful sessions and stand hours, in the order they arrived.
    """
    wanted = sorted(set(metrics))
    rows = conn.execute(
        f"SELECT {EVENT_COLUMNS} FROM events WHERE type = ? AND ts_epoch_ms > ? "  # noqa: S608 — the column list is a constant
        "AND ts_epoch_ms < ? AND json_extract(payload_json, '$.metric') IN "
        "(SELECT value FROM json_each(?)) ORDER BY id",
        (HEALTH_EVENT, start_ms, end_ms + SPAN_REACH_MS, json.dumps(wanted)),
    )
    events = [row_to_event(row) for row in rows]
    return [
        event for event in events
        if int(event.payload["start_ms"]) < end_ms and int(event.payload["end_ms"]) > start_ms
    ]


def last_before(
    conn: sqlite3.Connection, event_type: str, metric: str, at_ms: int, reach_ms: int,
) -> int | None:
    """When the latest row of ``metric`` before ``at_ms`` was logged; None if none within reach."""
    found = conn.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = ? AND ts_epoch_ms >= ? AND ts_epoch_ms < ?"
        " AND json_extract(payload_json, '$.metric') = ? ORDER BY ts_epoch_ms DESC LIMIT 1",
        (event_type, at_ms - reach_ms, at_ms, metric),
    ).fetchone()
    return None if found is None else int(found[0])


def first_after(
    conn: sqlite3.Connection, event_type: str, metric: str, at_ms: int, reach_ms: int,
) -> int | None:
    """When the first row of ``metric`` at or after ``at_ms`` was logged; None if none in reach."""
    found = conn.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = ? AND ts_epoch_ms >= ? AND ts_epoch_ms < ?"
        " AND json_extract(payload_json, '$.metric') = ? ORDER BY ts_epoch_ms LIMIT 1",
        (event_type, at_ms, at_ms + reach_ms, metric),
    ).fetchone()
    return None if found is None else int(found[0])


def watch_charge(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[Charge]:
    """The watch's charge readings taken in ``[start_ms, end_ms)``, by time (ADR 0377)."""
    rows = conn.execute(
        "SELECT ts_epoch_ms, payload_json FROM events WHERE type = ? AND ts_epoch_ms >= ? "
        "AND ts_epoch_ms < ? AND json_extract(payload_json, '$.signal') = ? ORDER BY ts_epoch_ms",
        (STATE_EVENT, start_ms, end_ms, WATCH_BATTERY),
    )
    found: list[Charge] = []
    for ts, raw in rows:
        payload = json.loads(raw)
        try:
            found.append(Charge(int(ts), int(payload["value"]), str(payload.get("detail") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return found


__all__ = [
    "SPAN_REACH_MS",
    "STATE_EVENT",
    "WATCH_BATTERY",
    "Charge",
    "Reading",
    "Total",
    "first_after",
    "last_before",
    "readings",
    "span_events",
    "totals",
    "watch_charge",
]
