"""ADR 0198: where a paired phone last said it was, read from the event log when asked.

On a brain there is no Mac to read, so "here" is the most recent place or location fix any phone
reported (ADR 0197). Nothing is cached: each call reads the log, and the answer carries its age.

A ``phone.location_observed`` is at its event time (``ts_epoch_ms``). A ``phone.visit_observed``
is at its departure when it has one (he is leaving there then) and at its arrival otherwise. The
latest of those times wins; on a tie the later row does.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.state.event_log import MAC_NODE, turn_origin

if TYPE_CHECKING:
    import sqlite3

LOCATION_EVENT: Final = "phone.location_observed"
VISIT_EVENT: Final = "phone.visit_observed"

_LATEST_SQL: Final = (
    "SELECT payload_json, ingestion_node, CASE type WHEN ? THEN COALESCE("
    "json_extract(payload_json, '$.departed_at_ms'), json_extract(payload_json, '$.arrived_at_ms')"
    ") ELSE ts_epoch_ms END AS at_ms FROM events WHERE type IN (?, ?) "
    "ORDER BY at_ms DESC, id DESC LIMIT 1"
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


def latest_phone_fix(conn: sqlite3.Connection) -> PhoneFix | None:
    """The latest place or location fix among all phones' reports, or ``None`` if none came."""
    row = conn.execute(_LATEST_SQL, (VISIT_EVENT, LOCATION_EVENT, VISIT_EVENT)).fetchone()
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
    )


def turn_from_phone(conn: sqlite3.Connection, turn_id: str | None) -> bool:
    """Whether a paired device opened ``turn_id``: its opening row was written under a device name.

    A Mac running alone has no terminal, so a device that talks to it is a phone (ADR 0212). A
    turn with no opening row, or none given, is the Mac's own.
    """
    node = turn_origin(conn, turn_id or "")[1]
    return node is not None and node != MAC_NODE


def read_phone_here(conn: sqlite3.Connection, *, now_ms: int | None = None) -> dict[str, Any]:
    """The phone's last report as the ``here`` reading: position, ``age_s``, ``source``, device.

    ``place`` is present only when the phone had one. ``age_s`` is whole seconds since the
    report puts him there (zero for a time a little ahead of the clock). There is no age
    cutoff: a phone that stays put sends nothing, so an old report is still where he is.

    Raises:
        NoPhoneReport: no phone has reported a location or a visit.
    """
    fix = latest_phone_fix(conn)
    if fix is None:
        msg = "no phone has reported a location"
        raise NoPhoneReport(msg)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    reading: dict[str, Any] = {
        "lat": fix.lat,
        "lng": fix.lng,
        "accuracy_m": fix.accuracy_m,
        "age_s": max(0, now - fix.at_ms) // 1000,
        "source": "phone",
        "device": fix.device,
    }
    if fix.place is not None:
        reading["place"] = fix.place
    return reading
