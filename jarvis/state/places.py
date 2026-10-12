"""L2 places he keeps: a versioned table in memory.db, and facts counted from his stays (ADR 0234).

``places`` is the current view and ``place_changes`` the log. Every write is one transaction
that appends a change (the place before and after, as JSON) and updates the view, so a change
can be listed and taken back like a core memory version (ADR 0154); an undo is itself a change
and is refused with :class:`~jarvis.state.memory_page.Conflict` when a later change touched the
same place. A place holds the name he gave, a role, a centre and radius that never move (or an
address alone until its centre is found, from the address once or where he is; ADR 0237) and
where it came from.

Facts are never stored: :func:`tally` counts them from the phone's stays (ADR 0197) each time.
Stays within :data:`SPOT_RADIUS_M` of a spot's running centre form that spot, in time order. A
stay inside a kept place's circle belongs to that place (the smallest circle wins) and to no
spot, so forgetting a place gives its visits back to the spots. Spots have no identity: a spot
is named in a listing by its centre rounded to four decimals (``spot:48.0012,-123.0034``), and
that id resolves to the nearest spot centre within :data:`SPOT_RADIUS_M` of it at the time it is
used. It holds for the conversation that read it, not across days.

Days are local days in the zone given; a stay counts for every day it overlaps. ``now_ms`` is
exclusive and ``today`` is the day of the last millisecond before it, so a count made at a local
midnight holds complete days only and does not move until the next midnight.

Errors: ``LookupError`` is a missing place or change, ``ValueError`` a refused input.
"""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.shared.geo import metres
from jarvis.state import core_memory
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.memory_db import iso_seconds, local_now, open_memory_db
from jarvis.state.memory_page import Conflict
from jarvis.state.phone_day import LOOKBACK_MS, Span, stays
from jarvis.state.phone_location import VISIT_EVENT

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

ROLES: Final[tuple[str, ...]] = ("home", "school", "other")
ORIGINS: Final[tuple[str, ...]] = ("user", "model", "settings", "page", "geocoder")
DEFAULT_RADIUS_M: Final[int] = 120
RADIUS_RANGE: Final[tuple[int, int]] = (50, 800)
NAME_CHARS: Final[int] = 40
SPOT_RADIUS_M: Final[float] = 100.0
"""Stays this close to a spot's running centre are that spot."""
RECENT_DAYS: Final[int] = 28
WINDOW_DAYS: Final[int] = 84
ASK_MIN_DAYS: Final[int] = 3
"""Days in the last four weeks before a spot is shown to her as one to ask about."""
CHANGE_LIMIT: Final[int] = 60
"""How many changes the page lists, newest first."""

_MINUTE_MS: Final[int] = 60_000
_SPOT_ID: Final[re.Pattern[str]] = re.compile(r"spot:(-?\d+\.\d+),(-?\d+\.\d+)")
_LAT_LNG: Final[re.Pattern[str]] = re.compile(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*")
_COLUMNS: Final[str] = (
    "id, name, role, lat, lng, radius_m, address, origin, created_at, updated_at"
)
_STATE_KEYS: Final[tuple[str, ...]] = (
    "name", "role", "lat", "lng", "radius_m", "address", "origin", "created_at", "updated_at",
)


@dataclass(frozen=True)
class Place:
    """One kept place: the current view row."""

    id: str
    name: str
    role: str
    lat: float | None
    lng: float | None
    radius_m: int
    address: str | None
    origin: str
    created_at: str
    updated_at: str

    def state(self) -> dict[str, Any]:
        """The row without its id: what a change stores as ``before`` and ``after``."""
        return {key: getattr(self, key) for key in _STATE_KEYS}


class Written(NamedTuple):
    """What a write did: the place after it (before it, for a forget) and the change appended."""

    place: Place
    change_id: str | None  # None when nothing needed changing


class Facts(NamedTuple):
    """What the program counted for one spot or kept place."""

    days_28: int
    days_84: int
    last_visit: date | None
    median_minutes: int | None
    label: str | None  # the phone's most common string for the stays


class Spot(NamedTuple):
    """A cluster of unkept stays: its centre, the id the listing gives it and its facts."""

    id: str
    lat: float
    lng: float
    facts: Facts


class Tally(NamedTuple):
    """Facts of every kept place by id, and every unkept spot, most visited first."""

    kept: dict[str, Facts]
    spots: list[Spot]


# --- the table ------------------------------------------------------------------------------


def _place(row: Sequence[Any]) -> Place:
    return Place(
        str(row[0]), str(row[1]), str(row[2]),
        None if row[3] is None else float(row[3]), None if row[4] is None else float(row[4]),
        int(row[5]), None if row[6] is None else str(row[6]), str(row[7]), str(row[8]),
        str(row[9]),
    )


def kept(conn: sqlite3.Connection) -> list[Place]:
    """Every kept place, oldest first."""
    return [
        _place(row)
        for row in conn.execute(f"SELECT {_COLUMNS} FROM places ORDER BY rowid")  # noqa: S608
    ]


def read_kept(path: Path) -> list[Place]:
    """The kept places read without creating anything; none when the file or table is not there."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            return kept(conn)
    except sqlite3.Error:
        return []


def stamp(path: Path) -> tuple[int, int]:
    """``(count, newest rowid)`` of the changes, (0, 0) when unreadable; keys a cache of names."""
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM place_changes")
            count, newest = row.fetchone()
            return int(count), int(newest)
    except sqlite3.Error:
        return 0, 0


def containing(places: Iterable[Place], lat: float, lng: float) -> Place | None:
    """The kept place whose circle holds the point; the smallest radius wins, then the older."""
    best: Place | None = None
    for one in places:
        if one.lat is None or one.lng is None:
            continue
        if (best is None or one.radius_m < best.radius_m) and (
            metres((lat, lng), (one.lat, one.lng)) <= one.radius_m
        ):
            best = one
    return best


def home_school(path: Path) -> dict[str, str]:
    """The kept home and school as `bus` reads them: ``lat,lng``, else the address.

    Read at each call. When two places share a role the newer one is the one he means.
    """
    found: dict[str, str] = {}
    for one in read_kept(path):
        if one.role == "other":
            continue
        if one.lat is not None and one.lng is not None:
            found[one.role] = f"{one.lat},{one.lng}"
        elif one.address:
            found[one.role] = one.address
    return found


def _name(text: str) -> str:
    name = " ".join(text.split())
    if not name:
        msg = "a place needs a name"
        raise ValueError(msg)
    if len(name) > NAME_CHARS:
        msg = f"a place's name is at most {NAME_CHARS} characters"
        raise ValueError(msg)
    return name


def _role(role: str) -> str:
    if role not in ROLES:
        msg = f"role must be one of {', '.join(ROLES)}"
        raise ValueError(msg)
    return role


def _radius(radius_m: int) -> int:
    low, high = RADIUS_RANGE
    if not low <= radius_m <= high:
        msg = f"radius_m is between {low} and {high}"
        raise ValueError(msg)
    return radius_m


def _centre(lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):  # noqa: PLR2004 — the globe
        msg = "that is not a latitude and longitude"
        raise ValueError(msg)


def _taken(conn: sqlite3.Connection, name: str, *, besides: str) -> Place | None:
    """Another kept place with this name (any case)."""
    return next(
        (p for p in kept(conn) if p.id != besides and p.name.casefold() == name.casefold()), None,
    )


def _append(  # noqa: PLR0913 — the change's columns, all required.
    conn: sqlite3.Connection, *, op: str, origin: str, before: Place | None, after: Place | None,
    now: str,
) -> str:
    """Update the view and append the change, inside the caller's transaction."""
    place = after or before
    assert place is not None  # noqa: S101 — a change has a side
    if after is None:
        conn.execute("DELETE FROM places WHERE id = ?", (place.id,))
    elif before is None:
        conn.execute(
            f"INSERT INTO places ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: S608
            (after.id, *(after.state()[key] for key in _STATE_KEYS)),
        )
    else:
        conn.execute(
            "UPDATE places SET name = ?, role = ?, lat = ?, lng = ?, radius_m = ?, address = ?, "
            "origin = ?, created_at = ?, updated_at = ? WHERE id = ?",
            (*(after.state()[key] for key in _STATE_KEYS), after.id),
        )
    change_id = f"place-change:{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO place_changes (id, ts, origin, op, place_id, before, after) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            change_id, now, origin, op, place.id,
            None if before is None else json.dumps(before.state(), ensure_ascii=False),
            None if after is None else json.dumps(after.state(), ensure_ascii=False),
        ),
    )
    return change_id


def _origin(origin: str) -> str:
    if origin not in ORIGINS:
        msg = f"origin must be one of {', '.join(ORIGINS)}"
        raise ValueError(msg)
    return origin


def save(  # noqa: PLR0913 — a place's columns and who is writing.
    path: Path, *, name: str, lat: float, lng: float, role: str | None = None,
    radius_m: int | None = None, origin: str,
) -> Written:
    """Keep the circle at ``lat, lng`` under ``name``.

    A name already kept without a centre (one that came as an address) gets this one; a spot that
    is already kept under that name is left as it is. A point inside a kept place's circle is
    refused unless the new circle is smaller (a library within a campus), and so is a name kept
    somewhere else: renaming is :func:`edit`'s.
    """
    label, origin = _name(name), _origin(origin)
    _centre(lat, lng)
    size = _radius(DEFAULT_RADIUS_M if radius_m is None else radius_m)
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        inside = containing(kept(conn), lat, lng)
        if inside is not None and size >= inside.radius_m:
            if inside.name.casefold() == label.casefold():
                return Written(inside, None)
            msg = (
                f"that spot is already kept as “{inside.name}” ({inside.id}); "
                "to rename it, pass its place id"
            )
            raise ValueError(msg)
        same = _taken(conn, label, besides="")
        if same is not None and same.lat is not None:
            msg = f"“{same.name}” ({same.id}) is already kept somewhere else"
            raise ValueError(msg)
        if same is not None:  # an address until now: he is there
            after = replace(
                same, lat=lat, lng=lng, updated_at=now,
                role=same.role if role is None else _role(role),
                radius_m=same.radius_m if radius_m is None else size,
            )
            done = _append(conn, op="save", origin=origin, before=same, after=after, now=now)
            return Written(after, done)
        made = Place(
            f"place:{uuid.uuid4().hex[:12]}", label, _role(role or "other"), lat, lng, size,
            None, origin, now, now,
        )
        done = _append(conn, op="save", origin=origin, before=None, after=made, now=now)
        return Written(made, done)


def edit(  # noqa: PLR0913 — what can change about a place, and who is writing.
    path: Path, place_id: str, *, name: str | None = None, role: str | None = None,
    radius_m: int | None = None, origin: str,
) -> Written:
    """Rename a kept place or change its role or radius; its centre never moves."""
    origin = _origin(origin)
    if name is None and role is None and radius_m is None:
        msg = "nothing to change: give a name, a role or a radius"
        raise ValueError(msg)
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        before = _one(conn, place_id)
        label = before.name if name is None else _name(name)
        clash = _taken(conn, label, besides=before.id)
        if clash is not None:
            msg = f"“{clash.name}” ({clash.id}) is already kept under that name"
            raise ValueError(msg)
        after = replace(
            before, name=label, updated_at=now,
            role=before.role if role is None else _role(role),
            radius_m=before.radius_m if radius_m is None else _radius(radius_m),
        )
        if after == replace(before, updated_at=now):
            return Written(before, None)
        done = _append(conn, op="rename", origin=origin, before=before, after=after, now=now)
        return Written(after, done)


def forget(path: Path, place_id: str, *, origin: str) -> Written:
    """Remove a kept place; its visits stay counted, as a spot's."""
    origin = _origin(origin)
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        before = _one(conn, place_id)
        done = _append(conn, op="forget", origin=origin, before=before, after=None, now=now)
        return Written(before, done)


def unlocated(path: Path) -> list[Place]:
    """Kept places with an address and no centre that no geocoder change has touched (ADR 0237).

    Read without creating anything; none when the file or table is not there.
    """
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM places WHERE lat IS NULL AND address IS NOT NULL "  # noqa: S608
                "AND id NOT IN (SELECT place_id FROM place_changes WHERE origin = 'geocoder') "
                "ORDER BY rowid",
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_place(row) for row in rows]


def locate(path: Path, place_id: str, *, lat: float, lng: float) -> Written:
    """Give a place kept as an address the centre found for it, as origin ``geocoder`` (ADR 0237).

    A place that has a centre by now (he named it on the spot) is left as it is. A point inside a
    kept place's circle is refused unless this place's circle is smaller, as :func:`save` does.
    """
    _centre(lat, lng)
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        before = _one(conn, place_id)
        if before.lat is not None:
            return Written(before, None)
        inside = containing(kept(conn), lat, lng)
        if inside is not None and before.radius_m >= inside.radius_m:
            msg = f"that point is inside “{inside.name}” ({inside.id})"
            raise ValueError(msg)
        after = replace(before, lat=lat, lng=lng, updated_at=now)
        done = _append(conn, op="locate", origin="geocoder", before=before, after=after, now=now)
        return Written(after, done)


def _one(conn: sqlite3.Connection, place_id: str) -> Place:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM places WHERE id = ?", (place_id,),  # noqa: S608
    ).fetchone()
    if row is None:
        msg = f"no kept place {place_id}"
        raise LookupError(msg)
    return _place(row)


def _restored(state: dict[str, Any], place_id: str) -> Place:
    return Place(place_id, **{key: state[key] for key in _STATE_KEYS})


def undo(path: Path, change_id: str, *, origin: str) -> Written:
    """Take a change back by appending the inverse; refused when a later one touched the place."""
    origin = _origin(origin)
    now = iso_seconds(local_now())
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        row = conn.execute(
            "SELECT rowid, place_id, before, after FROM place_changes WHERE id = ?", (change_id,),
        ).fetchone()
        if row is None:
            msg = f"no change {change_id}"
            raise LookupError(msg)
        rowid, place_id, raw_before, raw_after = row
        before = None if raw_before is None else _restored(json.loads(raw_before), place_id)
        after = None if raw_after is None else _restored(json.loads(raw_after), place_id)
        named = after or before
        assert named is not None  # noqa: S101 — a change has a side
        later = conn.execute(
            "SELECT 1 FROM place_changes WHERE place_id = ? AND rowid > ?", (place_id, rowid),
        ).fetchone()
        if later is not None:
            msg = f"a later change touched the same place (“{named.name}”); take that back first"
            raise Conflict(msg)
        if before is not None:
            clash = _taken(conn, before.name, besides=place_id)
            if clash is not None:
                msg = f"“{clash.name}” is kept now under that name; take that back first"
                raise Conflict(msg)
        done = _append(conn, op="undo", origin=origin, before=after, after=before, now=now)
        return Written(before or named, done)


def migrate_settings(path: Path, saved: Mapping[str, str]) -> list[Place]:
    """Copy the settings' ``home`` and ``school`` into places, once (ADR 0234).

    ``saved`` maps a role to ``lat,lng`` or an address. Nothing is copied when any change of
    origin ``settings`` exists, even one since forgotten, so the table is their only owner from
    then on.
    """
    now = iso_seconds(local_now())
    copied: list[Place] = []
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        done = conn.execute("SELECT 1 FROM place_changes WHERE origin = 'settings'").fetchone()
        if done is not None:
            return copied
        for role in ("home", "school"):
            text = saved.get(role, "").strip()
            if not text:
                continue
            found = _LAT_LNG.fullmatch(text)
            one = Place(
                f"place:{uuid.uuid4().hex[:12]}", role, role,
                float(found[1]) if found else None, float(found[2]) if found else None,
                DEFAULT_RADIUS_M, None if found else text, "settings", now, now,
            )
            _append(conn, op="save", origin="settings", before=None, after=one, now=now)
            copied.append(one)
    return copied


def changes(path: Path) -> list[dict[str, Any]]:
    """The latest :data:`CHANGE_LIMIT` changes, newest first, each with whether it can be undone."""
    with closing(open_memory_db(path)) as conn:
        rows = conn.execute(
            "SELECT id, ts, origin, op, place_id, before, after, rowid FROM place_changes "
            "ORDER BY rowid DESC",
        ).fetchall()
    newest: dict[str, int] = {}
    for row in rows:
        newest.setdefault(str(row[4]), int(row[7]))
    out: list[dict[str, Any]] = []
    for row in rows[:CHANGE_LIMIT]:
        before: dict[str, Any] | None = None if row[5] is None else json.loads(row[5])
        after: dict[str, Any] | None = None if row[6] is None else json.loads(row[6])
        out.append(
            {
                "id": str(row[0]), "ts": str(row[1]), "origin": str(row[2]), "op": str(row[3]),
                "place_id": str(row[4]), "name": (after or before or {}).get("name"),
                "before": before, "after": after, "undoable": newest[str(row[4])] == int(row[7]),
            },
        )
    return out


# --- the facts ------------------------------------------------------------------------------


def read_stays(
    conn: sqlite3.Connection, now_ms: int, zone: tzinfo, days: int = WINDOW_DAYS,
) -> list[Span]:
    """His stays over the last ``days`` days (the facts' window), from the log's visit rows."""
    today = _local_day(now_ms - 1, zone)
    first = datetime.combine(today - timedelta(days=days - 1), time.min, zone)
    since = int(first.timestamp() * 1000) - LOOKBACK_MS
    return stays(list(iter_events_of_types(conn, (VISIT_EVENT,), since_epoch_ms=since)))


def _local_day(ms: int, zone: tzinfo) -> date:
    return datetime.fromtimestamp(ms / 1000, zone).date()


def midnight_ms(now_ms: int, zone: tzinfo) -> int:
    """The local midnight that opens the day ``now_ms`` is in: where complete days end."""
    return int(datetime.combine(_local_day(now_ms, zone), time.min, zone).timestamp() * 1000)


class _Cluster:
    """The running centre of the stays that make a spot."""

    def __init__(self) -> None:
        self.members: list[Span] = []
        self._lat = 0.0
        self._lng = 0.0

    def add(self, stay: Span) -> None:
        self.members.append(stay)
        self._lat += float(stay.fields["lat"])
        self._lng += float(stay.fields["lng"])

    @property
    def centre(self) -> tuple[float, float]:
        return self._lat / len(self.members), self._lng / len(self.members)


def _facts(group: Sequence[Span], now_ms: int, zone: tzinfo) -> Facts:
    today = _local_day(now_ms - 1, zone)
    first = today - timedelta(days=WINDOW_DAYS - 1)
    recent = today - timedelta(days=RECENT_DAYS - 1)
    days: set[date] = set()
    lengths: list[float] = []
    labels: Counter[str] = Counter()
    last_seen: dict[str, int] = {}
    for stay in group:
        end = now_ms if stay.end is None else min(stay.end, now_ms)
        last = _local_day(max(end - 1, stay.start), zone)
        if last < first:
            continue
        day = max(_local_day(stay.start, zone), first)
        while day <= min(last, today):
            days.add(day)
            day += timedelta(days=1)
        if stay.end is not None and stay.end <= now_ms:
            lengths.append((stay.end - stay.start) / _MINUTE_MS)
        if label := stay.fields.get("place"):
            labels[label] += 1
            last_seen[label] = stay.start
    return Facts(
        sum(1 for d in days if d >= recent), len(days), max(days, default=None),
        round(statistics.median(lengths)) if lengths else None,
        max(labels, key=lambda s: (labels[s], last_seen[s])) if labels else None,
    )


def tally(
    seen: Sequence[Span], places: Sequence[Place], now_ms: int, zone: tzinfo,
) -> Tally:
    """Count ``seen`` (his stays, oldest first) into the kept places and the unkept spots.

    A stay that begins at or after ``now_ms`` is not counted, and one still going or ending after
    it is cut there.
    """
    held: dict[str, list[Span]] = {one.id: [] for one in places}
    clusters: list[_Cluster] = []
    for stay in seen:
        if stay.start >= now_ms:
            continue
        point = (float(stay.fields["lat"]), float(stay.fields["lng"]))
        home = containing(places, *point)
        if home is not None:
            held[home.id].append(stay)
            continue
        near = min(
            (c for c in clusters if metres(point, c.centre) <= SPOT_RADIUS_M),
            key=lambda c: metres(point, c.centre), default=None,
        )
        if near is None:
            near = _Cluster()
            clusters.append(near)
        near.add(stay)
    spots = [
        Spot(
            f"spot:{c.centre[0]:.4f},{c.centre[1]:.4f}", *c.centre,
            _facts(c.members, now_ms, zone),
        )
        for c in clusters
    ]
    spots = [s for s in spots if s.facts.days_84]
    spots.sort(
        key=lambda s: (s.facts.days_28, s.facts.days_84, s.facts.last_visit or date.min, s.id),
        reverse=True,
    )
    return Tally({pid: _facts(group, now_ms, zone) for pid, group in held.items()}, spots)


def spot_at(spots: Iterable[Spot], spot_id: str) -> Spot:
    """The spot a listing's id names: the nearest centre within :data:`SPOT_RADIUS_M` of it.

    Raises:
        LookupError: no spot is there now (spots move as stays are added).
    """
    found = _SPOT_ID.fullmatch(spot_id)
    if found is None:
        msg = f"{spot_id!r} is not a spot id"
        raise LookupError(msg)
    point = (float(found[1]), float(found[2]))
    near = min(spots, key=lambda s: metres(point, (s.lat, s.lng)), default=None)
    if near is None or metres(point, (near.lat, near.lng)) > SPOT_RADIUS_M:
        msg = f"no spot {spot_id} now; call places again"
        raise LookupError(msg)
    return near


def fact_view(facts: Facts) -> dict[str, Any]:
    """Facts as the tool and the page word them."""
    return {
        "days_28": facts.days_28, "days_84": facts.days_84,
        "last_visit": None if facts.last_visit is None else facts.last_visit.isoformat(),
        "median_stay_min": facts.median_minutes, "phone_label": facts.label,
    }


def place_view(one: Place, facts: Facts | None) -> dict[str, Any]:
    """A kept place with its facts, as the tool and the page word it."""
    return {
        "id": one.id, "name": one.name, "role": one.role, "radius_m": one.radius_m,
        "has_centre": one.lat is not None, "address": one.address, "origin": one.origin,
        **({} if facts is None else fact_view(facts)),
    }


def overview(path: Path, seen: Sequence[Span], now_ms: int, zone: tzinfo) -> dict[str, Any]:
    """The memory page's places: each kept place with its facts, and the latest changes."""
    with closing(open_memory_db(path)) as conn:
        held = kept(conn)
    counted = tally(seen, held, now_ms, zone).kept
    return {
        "places": [place_view(one, counted[one.id]) for one in held],
        "changes": changes(path),
    }


# --- what reads them ------------------------------------------------------------------------


def names_block(path: Path) -> str:
    """``[Places he keeps]`` with each place's name and role alone; empty when none is kept.

    The nightly core-memory pass reads it to leave out items that only restate a kept place. It
    needs no counts, so it reads no stays.
    """
    held = read_kept(path)
    if not held:
        return ""
    return "\n".join(["[Places he keeps]", *(f"{one.name} · {one.role}" for one in held)])


def prompt_block(path: Path, events: sqlite3.Connection, now_ms: int, zone: tzinfo) -> str:
    """The ``[Places he keeps]`` block of the system prompt; empty when none is kept.

    Counted at the local midnight, complete days only, so one text serves every turn of the day.
    """
    held = read_kept(path)
    if not held:
        return ""
    counted = tally(read_stays(events, now_ms, zone), held, midnight_ms(now_ms, zone), zone).kept
    lines = ["[Places he keeps]"]
    for one in held:
        facts = counted[one.id]
        if facts.days_28:
            weekly = round(facts.days_28 / (RECENT_DAYS / 7), 1)
            rate = f"~{weekly:g} days a week over the last 4 weeks"
        else:
            rate = "not visited in the last 4 weeks"
        tail = f" · last visit {facts.last_visit}" if facts.last_visit else ""
        lines.append(f"{one.name} · {one.role} · {rate}{tail}")
    return "\n".join(lines)


def ask_once(
    path: Path, events: sqlite3.Connection, fix: tuple[float, float], now_ms: int, zone: tzinfo,
) -> Spot | None:
    """The unkept spot the fix is in, once, if she has not been told of it before.

    The spot needs :data:`ASK_MIN_DAYS` days in the last four weeks before today, and no place
    kept or asked about may be within :data:`SPOT_RADIUS_M` of it. Returning it records it, so a
    spot is returned at most once.
    """
    held = read_kept(path)
    if containing(held, *fix) is not None:
        return None
    seen = read_stays(events, now_ms, zone, RECENT_DAYS)
    here = next(
        (
            s for s in tally(seen, held, midnight_ms(now_ms, zone), zone).spots
            if metres(fix, (s.lat, s.lng)) <= SPOT_RADIUS_M
        ),
        None,
    )
    if here is None or here.facts.days_28 < ASK_MIN_DAYS:
        return None
    with closing(open_memory_db(path)) as conn, core_memory.write_transaction(conn):
        asked = conn.execute("SELECT lat, lng FROM place_prompts").fetchall()
        if any(metres((here.lat, here.lng), (lat, lng)) <= SPOT_RADIUS_M for lat, lng in asked):
            return None
        conn.execute(
            "INSERT INTO place_prompts (id, lat, lng, shown_at) VALUES (?, ?, ?, ?)",
            (f"prompt:{uuid.uuid4().hex}", here.lat, here.lng, iso_seconds(local_now())),
        )
    return here
