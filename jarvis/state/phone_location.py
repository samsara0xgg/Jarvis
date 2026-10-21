"""ADR 0198, 0322: where a paired phone last said it was, read from the event log when asked.

:func:`latest_phone_fix` is the newest place or location fix a device reported (any phone when no
device is named); Here (ADR 0322) and the state block's place line are its only readers. Nothing
is cached: each call reads the log.

A ``phone.location_observed`` is at its event time (``ts_epoch_ms``). A ``phone.visit_observed``
is at its departure when it has one (he is leaving there then) and at its arrival otherwise. The
latest of those times wins; on a tie the later row does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.ride_progress import Fix

if TYPE_CHECKING:
    import sqlite3

LOCATION_EVENT: Final = "phone.location_observed"
VISIT_EVENT: Final = "phone.visit_observed"

_LATEST_SQL: Final = (
    "SELECT payload_json, ingestion_node, CASE type WHEN ? THEN COALESCE("
    "json_extract(payload_json, '$.departed_at_ms'), json_extract(payload_json, '$.arrived_at_ms')"
    ") ELSE ts_epoch_ms END AS at_ms FROM events WHERE type IN (?, ?) "
    "AND (? IS NULL OR ingestion_node = ?) ORDER BY at_ms DESC, id DESC LIMIT 1"
)


_FIXES_SQL: Final = (
    "SELECT payload_json, ts_epoch_ms, ingestion_node FROM events WHERE type = ? "
    "AND ts_epoch_ms >= ? ORDER BY ts_epoch_ms ASC, id ASC"
)
_MOTION_SQL: Final = (
    "SELECT payload_json FROM events WHERE type = 'phone.motion_observed' AND ts_epoch_ms >= ? "
    "ORDER BY ts_epoch_ms DESC, id DESC LIMIT 50"
)


class NoPhoneReport(LookupError):  # noqa: N818 - reads as the fact it states
    """No phone has reported a location, so the brain does not know where he is."""


@dataclass(frozen=True)
class PhoneFix:
    """The phone's latest report of where he is, and when it puts him there."""

    lat: float
    lng: float
    accuracy_m: float
    place: str | None
    at_ms: int
    device: str
    # A visit with a departure: he was leaving ``place`` at ``at_ms``.
    left: bool = False


def latest_phone_fix(conn: sqlite3.Connection, device: str | None = None) -> PhoneFix | None:
    """The latest place or location fix reported by ``device`` (any phone when ``None``).

    ``None`` when that device (or any phone) has reported none. A report from another device is
    never returned for a named one (ADR 0322).
    """
    row = conn.execute(
        _LATEST_SQL, (VISIT_EVENT, LOCATION_EVENT, VISIT_EVENT, device, device),
    ).fetchone()
    if row is None or row[2] is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return PhoneFix(
        lat=float(payload["lat"]),
        lng=float(payload["lng"]),
        accuracy_m=float(payload["accuracy_m"]),
        place=str(payload["place"]) if payload.get("place") else None,
        at_ms=int(row[2]),
        device=str(row[1]),
        left=payload.get("departed_at_ms") is not None,
    )


def fixes_since(conn: sqlite3.Connection, since_ms: int) -> list[Fix]:
    """The phones' location fixes timed at or after ``since_ms``, oldest first (ADR 0224).

    A fix is at the event's own time, which the phone sets; a visit is not a fix.
    """
    found: list[Fix] = []
    for raw, at_ms, node in conn.execute(_FIXES_SQL, (LOCATION_EVENT, since_ms)):
        payload: dict[str, Any] = json.loads(raw)
        found.append(Fix(
            int(at_ms), float(payload["lat"]), float(payload["lng"]),
            float(payload["accuracy_m"]), str(node),
        ))
    return found


def motion_now(conn: sqlite3.Connection, since_ms: int) -> str | None:
    """The activity of the newest motion segment that began at or after ``since_ms``, or None.

    Segments of low confidence are passed over. Newest means the latest start.
    """
    best: tuple[int, str] | None = None
    for (raw,) in conn.execute(_MOTION_SQL, (since_ms - 60_000,)):
        payload: dict[str, Any] = json.loads(raw)
        started = int(payload["started_at_ms"])
        if started >= since_ms and payload["confidence"] != "low" and (
            best is None or started > best[0]
        ):
            best = (started, str(payload["activity"]))
    return None if best is None else best[1]


def visit_position(conn: sqlite3.Connection, words: str) -> tuple[float, float] | None:
    """Where the phone last put him at a place whose name holds ``words`` (any case), or None."""
    escaped = words.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"
    row = conn.execute(
        "SELECT payload_json FROM events WHERE type = ? "
        "AND lower(json_extract(payload_json, '$.place')) LIKE ? ESCAPE '\\' "
        "ORDER BY ts_epoch_ms DESC, id DESC LIMIT 1",
        (VISIT_EVENT, like),
    ).fetchone()
    if row is None:
        return None
    payload: dict[str, Any] = json.loads(row[0])
    return float(payload["lat"]), float(payload["lng"])
