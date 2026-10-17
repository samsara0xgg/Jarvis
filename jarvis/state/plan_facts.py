"""What the log says about a day as of a moment, for the plan watcher (ADR 0289).

:func:`facts_at` reads only events timed at or before ``now_ms``, so a past day can be replayed
minute by minute and a notice means the same thing live and in replay. It reuses the phone folds
the day line and the ledger count by (:mod:`jarvis.state.phone_day`), so where he stayed is the
same fact everywhere. Nothing is stored here and nothing calls a model; deciding what a fact means
for the plan is :mod:`jarvis.decision.plan_watch`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import places
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.phone_day import LOOKBACK_MS, read_rows, stays
from jarvis.state.phone_day import moves as phone_moves
from jarvis.state.phone_location import LOCATION_EVENT

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import tzinfo

    from jarvis.shared import Event

WAKE_FROM: Final[time] = time(4, 0)
"""Signals before this local time belong to the night, not to waking."""
FIX_LOOKBACK_MS: Final[int] = 6 * 3_600_000
"""How far back the latest location fix is looked for."""
WORK_LOOKBACK_MS: Final[int] = 2 * 24 * 3_600_000
MAX_ACTIVITIES: Final[int] = 3
TEXT_CHARS: Final[int] = 230

_PHONE_WAKE: Final[dict[tuple[str, str], str]] = {
    ("focus", "off"): "focus_off",
    ("alarm", "stopped"): "alarm_stopped",
    ("alarm", "snoozed"): "alarm_snoozed",
    ("power", "disconnected"): "charger_unplugged",
}
_MAC_ACTIVE: Final[str] = "active"
_WATCHED: Final[tuple[str, ...]] = (
    "phone.state_observed", "timesink.state_observed", "utterance.received",
)


@dataclass(frozen=True)
class Wake:
    """The first sign of the day that he is up: its ``kind`` and when it happened."""

    # focus_off, alarm_stopped, alarm_snoozed, charger_unplugged, mac_active or utterance
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
    """A stretch of walking, running, cycling or driving. ``end_ms`` None: still going."""

    start_ms: int
    end_ms: int | None
    activity: str


@dataclass(frozen=True)
class NotedCommitment:
    """A commitment noted today for today. ``start`` is ``HH:MM`` local, empty when all day."""

    commitment_id: str
    title: str
    start: str
    place: str
    private: bool
    noted_at_ms: int


@dataclass(frozen=True)
class DayFacts:
    """Everything the watcher may know about ``day`` at ``now_ms``."""

    now_ms: int
    wake: Wake | None
    here: Here | None
    stays: tuple[Stay, ...]
    moves: tuple[Move, ...]
    doing: tuple[str, ...]
    commitments: tuple[NotedCommitment, ...]

    def moves_since(self, since_ms: int) -> tuple[Move, ...]:
        """The moves that were going at or after ``since_ms``."""
        return tuple(m for m in self.moves if m.end_ms is None or m.end_ms >= since_ms)

    def payload(self, zone: tzinfo) -> dict[str, Any]:
        """The facts as JSON-able values with local ``HH:MM`` times, for a prompt or a log line."""

        def clock(ms: int | None) -> str | None:
            return None if ms is None else f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"

        here = self.here
        return {
            "wake": self.wake and {"kind": self.wake.kind, "at": clock(self.wake.at_ms)},
            "here": here and {
                "place": here.place, "since": clock(here.since_ms),
                "fix_age_min": None if here.fix_age_ms is None else here.fix_age_ms // 60_000,
            },
            "stays": [
                {"place": s.kept or s.place, "from": clock(s.start_ms), "to": clock(s.end_ms)}
                for s in self.stays
            ],
            "moves": [
                {"activity": m.activity, "from": clock(m.start_ms), "to": clock(m.end_ms)}
                for m in self.moves
            ],
            "doing": list(self.doing),
            "commitments_noted_today": [
                {"title": c.title, "start": c.start or None, "place": c.place,
                 "noted_at": clock(c.noted_at_ms)}
                for c in self.commitments
            ],
        }


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
        elif event.type == "utterance.received":
            found.append(Wake("utterance", event.ts_epoch_ms))
        else:
            latest = payload.get("state_latest")
            if isinstance(latest, dict) and latest.get("kind") == _MAC_ACTIVE:
                # the state event's own time: the observer polls every few minutes
                at = int(datetime.fromisoformat(str(latest["at"])).timestamp() * 1000)
                if first_ms <= at <= now_ms:
                    found.append(Wake("mac_active", at))
    return min(found, key=lambda w: w.at_ms, default=None)


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


def _doing(conn: sqlite3.Connection, now_ms: int) -> tuple[str, ...]:
    """The activities of the latest work state before ``now_ms``, as the ledger words them."""
    latest: Any = None
    for event in iter_events_of_types(
        conn, ("work_state.revised",), since_epoch_ms=now_ms - WORK_LOOKBACK_MS,
    ):
        if event.ts_epoch_ms <= now_ms and isinstance(event.payload.get("item"), dict):
            latest = event.payload["item"]
    if latest is None:
        return ()
    texts = (str(a.get("text") or "") for a in latest.get("activities") or [])
    return tuple(" ".join(t.split())[:TEXT_CHARS] for t in texts if t)[:MAX_ACTIVITIES]


def _noted(  # noqa: PLR0913 - the day, its bounds, the moment and the zone.
    conn: sqlite3.Connection, day: date, first_ms: int, last_ms: int, now_ms: int, zone: tzinfo,
) -> tuple[NotedCommitment, ...]:
    """Commitments noted during ``day`` (up to ``now_ms``) that fall on ``day`` and still stand."""
    dropped: set[str] = set()
    noted: dict[str, NotedCommitment] = {}
    for event in iter_events_of_types(
        conn, ("commitment.noted", "commitment.dropped"), since_epoch_ms=first_ms,
    ):
        if event.ts_epoch_ms > min(now_ms, last_ms - 1):
            continue
        payload = event.payload
        cid = str(payload["commitment_id"])
        if event.type == "commitment.dropped":
            dropped.add(cid)
            continue
        start = datetime.fromisoformat(str(payload["start_at"])).astimezone(zone)
        if start.date() != day:
            continue
        all_day = bool(payload.get("all_day"))
        noted[cid] = NotedCommitment(
            cid, str(payload["title"]), "" if all_day else f"{start:%H:%M}",
            str(payload.get("place") or ""), bool(payload["private"]), event.ts_epoch_ms,
        )
    return tuple(c for cid, c in noted.items() if cid not in dropped)


def facts_at(
    conn: sqlite3.Connection, day: date, now_ms: int, zone: tzinfo, kept: Sequence[places.Place],
) -> DayFacts:
    """The facts about ``day`` as the log held them at ``now_ms`` (events timed after are unseen).

    ``kept`` are the places he keeps (``places.read_kept``); a stay or fix inside one carries its
    name and role. ``wake`` is the earliest of focus off, alarm stopped or snoozed, charger
    unplugged, the Mac turning active and an utterance of his, after 04:00 local.
    """
    first_ms, last_ms = _ms(day, time.min, zone), _ms(day + timedelta(days=1), time.min, zone)
    rows = read_rows(conn, first_ms - LOOKBACK_MS)
    visits, motions = _until(rows.visits, now_ms), _until(rows.motions, now_ms)
    held = stays(visits)
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
    drive = tuple(
        Move(m.start, m.end, str(m.fields["activity"]))
        for m in phone_moves(motions, held, now_ms)
        if m.start <= now_ms and (m.end is None or m.end >= first_ms)
    )
    return DayFacts(
        now_ms=now_ms,
        wake=_wake(conn, _ms(day, WAKE_FROM, zone), now_ms),
        here=here,
        stays=all_stays,
        moves=drive,
        doing=_doing(conn, now_ms),
        commitments=_noted(conn, day, first_ms, last_ms, now_ms, zone),
    )

