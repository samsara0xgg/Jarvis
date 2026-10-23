"""What the log says about a day as of a moment, for the plan watcher (ADR 0383).

:func:`facts_at` reads only events timed at or before ``now_ms``, so a past day can be replayed
minute by minute and a line is due, or not, the same way live and in replay. It reuses the phone
folds the day line and the ledger count by (:mod:`jarvis.state.phone_day`), so where he stayed is
the same fact everywhere. Nothing is stored here and nothing calls a model; deciding what a fact
means for the plan is :mod:`jarvis.decision.plan_gate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import places, rides
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.phone_day import LOOKBACK_MS, read_rows, stays
from jarvis.state.phone_day import moves as phone_moves
from jarvis.state.phone_location import LOCATION_EVENT

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence
    from datetime import tzinfo

    from jarvis.shared import Event

WAKE_FROM: Final[time] = time(4, 0)
"""Signals before this local time belong to the night, not to waking."""
FIX_LOOKBACK_MS: Final[int] = 6 * 3_600_000
"""How far back the latest location fix is looked for."""

_PHONE_WAKE: Final[dict[tuple[str, str], str]] = {
    ("focus", "off"): "focus_off",
    ("alarm", "stopped"): "alarm_stopped",
    ("alarm", "snoozed"): "alarm_snoozed",
    ("power", "disconnected"): "charger_unplugged",
    ("headphones", "connected"): "headphones_on",
    ("call", "started"): "call",
}
_MAC_RESUMED: Final[dict[str, str]] = {"active": "idle", "unlock": "lock", "wake": "sleep"}
"""TimeSink's states for the Mac in use again, each with the stretch it ends: back from idle,
unlocked, woken from sleep."""
_MAC_TRACKING: Final[frozenset[str]] = frozenset(
    {"start", "stop", "tracking_pause", "tracking_resume"},
)
"""TimeSink's own marks: what it said about the Mac before them may no longer hold."""
_WATCHED: Final[tuple[str, ...]] = (
    "phone.state_observed", "timesink.state_observed", "utterance.received",
    "surface.user_intent", rides.STARTED,
)
_SPOKEN: Final[tuple[str, ...]] = ("utterance.received", "surface.user_intent")
_THING_EVENTS: Final[tuple[str, ...]] = ("thing.added", "thing.moved", "thing.closed")
_COUNTED: Final[tuple[str, ...]] = ("mail", "mirror")
"""The writers of a thing that is news to him: his own words are never told back to him."""
_TIME_KEYS: Final[tuple[str, ...]] = ("start_at", "on_day", "due")


@dataclass(frozen=True)
class Wake:
    """The first sign of the day that he is up: its ``kind`` and when it happened."""

    # a phone signal (focus_off, alarm_stopped, ..., headphones_on, call), mac_active,
    # utterance (his words, spoken or typed), ride, or move (the first walk or drive)
    kind: str
    at_ms: int


@dataclass(frozen=True)
class Stay:
    """One phone stay. ``end_ms`` None: he is still there. ``kept`` is a place he keeps, if any."""

    start_ms: int
    end_ms: int | None
    place: str  # the phone's own string for it
    kept: str
    kept_role: str
    lat: float
    lng: float


@dataclass(frozen=True)
class Here:
    """Where he is: in a stay (``since_ms``) or at the latest fix (``fix_age_ms`` old)."""

    place: str  # the kept place's name, else the phone's string, else ""
    kept: str
    kept_role: str
    lat: float
    lng: float
    since_ms: int | None
    fix_age_ms: int | None

    @property
    def in_stay(self) -> bool:
        """True when this is a phone stay, not a bare fix."""
        return self.since_ms is not None


@dataclass(frozen=True)
class Move:
    """A stretch of walking, running, cycling, driving or ``moving`` (ADR 0316); no end: going."""

    start_ms: int
    end_ms: int | None
    activity: str


@dataclass(frozen=True)
class MacAway:
    """A stretch the Mac was idle, locked or asleep (TimeSink's marks); no end: it still is."""

    start_ms: int
    end_ms: int | None
    kind: str  # idle | lock | sleep


@dataclass(frozen=True)
class NotedThing:
    """A thing added or moved today that falls on today or tomorrow.

    ``start`` is ``HH:MM`` local, empty when it has no hour; ``day`` is the date it falls on;
    ``by`` is who wrote it last (``thing.added`` or ``thing.moved``) and ``noted_at_ms`` when.
    """

    thing_id: str
    title: str
    start: str
    day: date
    place: str
    private: bool
    by: str
    noted_at_ms: int


@dataclass(frozen=True)
class DayFacts:
    """Everything the watcher may know about ``day`` at ``now_ms``."""

    now_ms: int
    wake: Wake | None
    here: Here | None
    stays: tuple[Stay, ...]
    moves: tuple[Move, ...]
    things: tuple[NotedThing, ...]
    mac_away: tuple[MacAway, ...] = ()


def _ms(day: date, at: time, zone: tzinfo) -> int:
    return int(datetime.combine(day, at, tzinfo=zone).timestamp() * 1000)


def _until(events: Sequence[Event], now_ms: int) -> list[Event]:
    return [e for e in events if e.ts_epoch_ms <= now_ms]


def _wake(conn: sqlite3.Connection, first_ms: int, now_ms: int) -> Wake | None:
    """The earliest wake signal in ``[first_ms, now_ms]``."""
    found: list[Wake] = []
    for event in iter_events_of_types(conn, _WATCHED, since_epoch_ms=first_ms):
        if event.ts_epoch_ms > now_ms:
            continue
        payload = event.payload
        if event.type == "phone.state_observed":
            kind = _PHONE_WAKE.get((str(payload["signal"]), str(payload["value"])))
            if kind:
                found.append(Wake(kind, event.ts_epoch_ms))
        elif event.type in _SPOKEN:
            found.append(Wake("utterance", event.ts_epoch_ms))
        elif event.type == rides.STARTED:
            found.append(Wake("ride", event.ts_epoch_ms))
        else:
            latest = payload.get("state_latest")
            if isinstance(latest, dict) and latest.get("kind") in _MAC_RESUMED:
                # the state event's own time: the observer polls every few minutes
                at = int(datetime.fromisoformat(str(latest["at"])).timestamp() * 1000)
                if first_ms <= at <= now_ms:
                    found.append(Wake("mac_active", at))
    return min(found, key=lambda w: w.at_ms, default=None)


def _rides(conn: sqlite3.Connection, since_ms: int, now_ms: int) -> list[Move]:
    """His bus rides (ADR 0221's follower) as moves: a ride is leaving, like a walk or a drive."""
    begun: dict[str, int] = {}
    ended: dict[str, int] = {}
    for event in iter_events_of_types(conn, (rides.STARTED, rides.ENDED), since_epoch_ms=since_ms):
        if event.ts_epoch_ms > now_ms:
            continue
        ride = str(event.payload["ride_id"])
        (begun if event.type == rides.STARTED else ended)[ride] = event.ts_epoch_ms
    return [Move(at, ended.get(ride), "ride") for ride, at in begun.items()]


def _kept_of(kept: Sequence[places.Place], lat: float, lng: float) -> tuple[str, str]:
    inside = places.containing(kept, lat, lng)
    return (inside.name, inside.role) if inside else ("", "")


def _latest_fix(conn: sqlite3.Connection, now_ms: int) -> Event | None:
    fix = None
    for event in iter_events_of_types(
        conn, (LOCATION_EVENT,), since_epoch_ms=now_ms - FIX_LOOKBACK_MS,
    ):
        if event.ts_epoch_ms <= now_ms:
            fix = event
    return fix


def _mac_away(conn: sqlite3.Connection, first_ms: int, now_ms: int) -> tuple[MacAway, ...]:
    """The stretches the Mac was idle, locked or asleep, from TimeSink's state marks.

    Each ``timesink.state_observed`` carries the latest mark; a mark is read once, at its own time.
    A resume ends the stretch it belongs to, and TimeSink's own marks end them all.
    """
    marks: set[tuple[int, str]] = set()
    for event in iter_events_of_types(conn, ("timesink.state_observed",), since_epoch_ms=first_ms):
        latest = event.payload.get("state_latest")
        if event.ts_epoch_ms <= now_ms and isinstance(latest, dict):
            at = int(datetime.fromisoformat(str(latest["at"])).timestamp() * 1000)
            if at <= now_ms:
                marks.add((at, str(latest["kind"])))
    began: dict[str, int] = {}
    found: list[MacAway] = []
    for at, kind in sorted(marks):
        if kind in _MAC_TRACKING:
            found += [MacAway(since, at, away) for away, since in began.items()]
            began.clear()
        elif kind in _MAC_RESUMED.values():
            began.setdefault(kind, at)
        elif kind in _MAC_RESUMED and _MAC_RESUMED[kind] in began:
            found.append(MacAway(began.pop(_MAC_RESUMED[kind]), at, _MAC_RESUMED[kind]))
    found += [MacAway(since, None, away) for away, since in began.items()]
    return tuple(sorted(found, key=lambda a: a.start_ms))


def _hour_on(fields: Mapping[str, Any], day: date, zone: tzinfo) -> str | None:
    """``HH:MM`` local (empty for a whole day) when the time in ``fields`` falls on ``day``.

    ``fields`` hold a thing's ``start_at``, ``on_day`` and ``due``; None when it falls on another
    day or has no time.
    """
    if fields["start_at"]:
        at = datetime.fromisoformat(fields["start_at"]).astimezone(zone)
        return f"{at:%H:%M}" if at.date() == day else None
    if fields["on_day"]:
        return "" if fields["on_day"] == day.isoformat() else None
    due = fields["due"]
    if len(due) == len(day.isoformat()):  # a date alone: by the end of that day
        return "" if due == day.isoformat() else None
    if due:
        at = datetime.fromisoformat(due).astimezone(zone)
        return f"{at:%H:%M}" if at.date() == day else None
    return None


def _held(
    day: date, seen: Sequence[Event], earlier: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The things the events of ``day`` leave standing, with who wrote them last and when."""
    held: dict[str, dict[str, Any]] = {}
    for event in seen:
        payload = event.payload
        thing_id = str(payload["thing_id"])
        if event.type == "thing.added":
            if payload["by"] != "carry_in":
                held[thing_id] = {
                    **{k: str(payload.get(k) or "") for k in _TIME_KEYS},
                    "title": str(payload["title"]), "place": str(payload.get("place") or ""),
                    "private": bool(payload.get("private")), "by": str(payload["by"]),
                    "at": event.ts_epoch_ms,
                }
        elif event.type == "thing.moved" and not payload.get("date"):
            if thing_id not in held and thing_id in earlier:
                base = earlier[thing_id]
                held[thing_id] = {
                    "title": str(base["title"]), "place": str(base.get("place") or ""),
                    "private": bool(base.get("private")),
                }
            if thing_id in held:
                held[thing_id] |= {k: str(payload[k]) for k in _TIME_KEYS}
                held[thing_id] |= {"by": str(payload["by"]), "at": event.ts_epoch_ms}
        elif thing_id in held and event.type == "thing.closed" and (
            not payload.get("date") or payload["date"] == day.isoformat()
        ):
            del held[thing_id]
    return held


def _noted(  # noqa: PLR0913 - the day, its bounds, the moment and the zone.
    conn: sqlite3.Connection, day: date, first_ms: int, last_ms: int, now_ms: int, zone: tzinfo,
) -> tuple[NotedThing, ...]:
    """Things added or moved during ``day`` (up to ``now_ms``) that fall on ``day`` or the next.

    One the carry-in wrote is not new. A move today counts where it lands, by whoever moved it,
    at the move's time, whether the thing was added today or before; a thing closed today
    (cancelled, done or paid, or that date of a weekly one) no longer stands.
    """
    seen = [
        e for e in iter_events_of_types(conn, _THING_EVENTS, since_epoch_ms=first_ms)
        if e.ts_epoch_ms <= min(now_ms, last_ms - 1)
    ]
    added = {str(e.payload["thing_id"]) for e in seen if e.type == "thing.added"}
    moved = {str(e.payload["thing_id"]) for e in seen if e.type == "thing.moved"} - added
    earlier = {
        str(e.payload["thing_id"]): e.payload
        for e in iter_events_of_types(conn, ("thing.added",))
        if e.ts_epoch_ms < first_ms and e.payload["thing_id"] in moved
    }
    found = []
    for thing_id, one in _held(day, seen, earlier).items():
        for falls in (day, day + timedelta(days=1)):
            if (hour := _hour_on(one, falls, zone)) is not None:
                found.append(NotedThing(
                    thing_id, one["title"], hour, falls, one["place"], one["private"], one["by"],
                    one["at"],
                ))
                break
    return tuple(found)


def facts_at(
    conn: sqlite3.Connection, day: date, now_ms: int, zone: tzinfo, kept: Sequence[places.Place],
) -> DayFacts:
    """The facts about ``day`` as the log held them at ``now_ms`` (events timed after are unseen).

    ``kept`` are the places he keeps (``places.read_kept``); a stay or fix inside one carries its
    name and role. ``wake`` is the earliest sign after 04:00 local that he is up: a phone signal
    (focus off, alarm stopped or snoozed, charger unplugged, headphones on, a call), the Mac
    turning active, words of his, a ride, or a walk or drive. ``moves`` hold his rides too.
    ``mac_away`` are the Mac's idle, locked and asleep stretches of ``day``.
    """
    first_ms, last_ms = _ms(day, time.min, zone), _ms(day + timedelta(days=1), time.min, zone)
    rows = read_rows(conn, first_ms - LOOKBACK_MS)
    seen = rows._replace(
        visits=_until(rows.visits, now_ms), motions=_until(rows.motions, now_ms),
        fixes=_until(rows.fixes, now_ms),
    )
    held = stays(seen, now_ms)
    all_stays = tuple(
        Stay(s.start, s.end, str(s.fields.get("place") or ""), *_kept_of(
            kept, s.fields["lat"], s.fields["lng"]), s.fields["lat"], s.fields["lng"])
        for s in held if s.start <= now_ms and (s.end is None or s.end >= first_ms)
    )
    current = all_stays[-1] if all_stays and all_stays[-1].end_ms is None else None
    here: Here | None = None
    if current is not None:
        here = Here(
            current.kept or current.place, current.kept, current.kept_role, current.lat,
            current.lng, current.start_ms, None,
        )
    elif (fix := _latest_fix(conn, now_ms)) is not None:
        lat, lng = float(fix.payload["lat"]), float(fix.payload["lng"])
        name, role = _kept_of(kept, lat, lng)
        here = Here(
            name or str(fix.payload.get("place") or ""), name, role, lat, lng, None,
            now_ms - fix.ts_epoch_ms,
        )
    morning = _ms(day, WAKE_FROM, zone)
    drive = tuple(sorted(
        (
            *(Move(m.start, m.end, str(m.fields.get("activity", "moving")))
              for m in phone_moves(seen, held, now_ms)
              if m.start <= now_ms and (m.end is None or m.end >= first_ms)),
            *_rides(conn, first_ms, now_ms),
        ),
        key=lambda m: m.start_ms,
    ))
    wake = _wake(conn, morning, now_ms)
    walked = next((m for m in drive if m.start_ms >= morning), None)
    if walked is not None and (wake is None or walked.start_ms < wake.at_ms):
        wake = Wake("move", walked.start_ms)
    return DayFacts(
        now_ms=now_ms,
        wake=wake,
        here=here,
        stays=all_stays,
        moves=drive,
        things=_noted(conn, day, first_ms, last_ms, now_ms, zone),
        mac_away=_mac_away(conn, first_ms, now_ms),
    )

