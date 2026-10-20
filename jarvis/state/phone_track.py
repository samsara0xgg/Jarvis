"""ADR 0312: every fix a phone kept while its location updates ran, read over a window.

A ``phone.track_observed`` is one stretch of a run of updates, at most ten minutes (the phone
sends a minute at a time): ``started_at_ms`` and ``ended_at_ms`` are its first and last fix, and
``fixes`` holds every fix iOS gave in between, each a row of :data:`FIX_COLUMNS` whose ``t_ms``
counts from ``started_at_ms``. A value iOS reported as invalid is ``None``; ``flags`` says whether
iOS marked the fix as simulated by software or as produced by an accessory. The sampled fixes of
``phone.location_observed`` are what every reader of where he is uses; this is read only when a
reader asks for a window of the track.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import sqlite3

TRACK_EVENT: Final = "phone.track_observed"

FIX_COLUMNS: Final = (
    "t_ms", "lat", "lng", "accuracy_m", "altitude_m", "vertical_accuracy_m", "speed_mps",
    "speed_accuracy_mps", "course_deg", "course_accuracy_deg", "flags",
)
"""One fix of a frame, in this order: the offset from ``started_at_ms`` (integer ms), degrees,
metres, metres a second, degrees clockwise from true north, and the flag bits."""

SIMULATED: Final = 1
"""``flags`` bit: iOS says software simulated the fix (``isSimulatedBySoftware``)."""
FROM_ACCESSORY: Final = 2
"""``flags`` bit: iOS says an external accessory produced the fix (``isProducedByAccessory``)."""

COLUMNS: Final = ("at_ms", *FIX_COLUMNS[1:])
"""A fix as :func:`track` gives it: its UTC millisecond in place of the offset."""

MAX_WINDOW_MS: Final = 12 * 60 * 60 * 1000
"""The longest window one read covers: a day of riding at a fix a second is 86,400 rows."""

_TRACK_SQL: Final = (
    "SELECT payload_json, ingestion_node FROM events WHERE type = ? AND ts_epoch_ms >= ? "
    "AND json_extract(payload_json, '$.ended_at_ms') >= ? "
    "AND json_extract(payload_json, '$.started_at_ms') <= ? ORDER BY id ASC"
)
"""A frame's ``ts_epoch_ms`` is its last fix, or the arrival time when the brain clamped it. A
fix is never more than five minutes ahead of the brain's clock, so ``ts_epoch_ms`` is at most
:data:`_SLACK_MS` before the frame's end and bounds the scan from below."""
_SLACK_MS: Final = 5 * 60 * 1000


def track(conn: sqlite3.Connection, from_ms: int, to_ms: int) -> list[dict[str, Any]]:
    """Each device's fixes from ``from_ms`` to ``to_ms`` (inclusive), oldest first.

    ``[{"device", "fixes": [[at_ms, lat, ...], ...]}]`` in :data:`COLUMNS` order, devices by
    name. A fix that two frames both hold is given once. A ValueError for a window that ends
    before it starts or is longer than :data:`MAX_WINDOW_MS`.
    """
    if to_ms < from_ms:
        msg = "the window ends before it starts"
        raise ValueError(msg)
    if to_ms - from_ms > MAX_WINDOW_MS:
        msg = "the window is longer than 12 hours"
        raise ValueError(msg)
    by_device: dict[str, dict[int, list[Any]]] = {}
    for raw, device in conn.execute(
        _TRACK_SQL, (TRACK_EVENT, from_ms - _SLACK_MS, from_ms, to_ms),
    ):
        payload = json.loads(raw)
        start = int(payload["started_at_ms"])
        fixes = by_device.setdefault(str(device), {})
        for fix in payload["fixes"]:
            at = start + int(fix[0])
            if from_ms <= at <= to_ms:
                fixes.setdefault(at, [at, *fix[1:]])
    return [
        {"device": device, "fixes": [fixes[at] for at in sorted(fixes)]}
        for device, fixes in sorted(by_device.items()) if fixes
    ]
