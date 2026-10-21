"""ADR 0323: his footprint of a day, the outings between his long stays, each with its route.

Acceptance checks against the real route, the real event log and the real memory.db. A phone's
frames go in through ``POST /inherent/device/events`` as they do from his phone, and
``GET /inherent/footprint`` folds them back:

- the outings: one drive between two long home stays with three short stops is one outing with
  three stops; a stay of 25 minutes splits it in two, and so does one of 8 minutes in a place he
  keeps; a stretch is an outing only with a move in it or 300 m between its stays; before the first
  long stay it starts at the first move, and with none after it it is still going; across midnight
  it is whole on both days; the stays carry the day line's ids;
- the route: the device with the most fixes, its track and the sampled fixes where the track has
  none, a bad or simulated fix left out, run on into the stay after while it still closes in, a
  gap reported with the stops in it left out and its straight line counted, the distance from
  stay to stay, speeds as 10 s means with the top speed their highest, and a simplification that
  leaves no dropped fix more than 5 m from the kept points;
- heart rate against the same day at his kept home, awake;
- the route itself: no date is today at the brain's clock in his zone, even when it is
  already tomorrow in UTC, a bad date is a 400, and the guard is the day's.

Every position is made up, in metres from an origin that is nowhere in particular, and every fix
lies where the test draws it, so a distance or a speed is plain arithmetic.
"""

from __future__ import annotations

import functools
import json
import math
from bisect import bisect_left
from contextlib import closing
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime.daemon import routes
from jarvis.shared.geo import metres
from jarvis.state import places
from jarvis.state.day_line import day_window
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import open_memory_db
from jarvis.state.phone_track import SIMULATED
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key
from jarvis.surface.phone_events import MAX_BATCH_BYTES, MAX_BATCH_FRAMES, PHONE_EVENTS_PATH
from jarvis.surface.terminal_events import BrainEvents
from tests.support.clock import FakeClock
from tests.support.terminal import brain_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"
VANCOUVER = "America/Vancouver"
ZONE = ZoneInfo(VANCOUVER)
SECOND = 1000
MINUTE = 60 * SECOND
HOUR = 60 * MINUTE
VISIT, MOTION = "phone.visit_observed", "phone.motion_observed"
SAMPLED, MEASURE = "phone.location_observed", "phone.measure_observed"
HEALTH = "phone.health_observed"
TRACK = "phone.track_observed"
FOOTPRINT = "/inherent/footprint"
DAY = datetime.now(ZONE).date() - timedelta(days=2)  # past, and inside the week a phone is trusted
AFTERWARDS = datetime(DAY.year, DAY.month, DAY.day, tzinfo=ZONE) + timedelta(days=2)
"""The brain's clock unless a test sets one: after every day the tests draw."""
EVENING = datetime(DAY.year, DAY.month, DAY.day, 17, 30, tzinfo=ZONE)
"""Half past five on ``DAY``: his day still, and already the next one in UTC."""
OUTING_KEYS = {
    "id", "start_ms", "end_ms", "from_stay", "to_stay", "activity", "distance_m", "moving_ms",
    "max_speed_mps", "stops", "gaps", "heart_rate", "points",
}
STAY_KEYS = {"id", "start_ms", "end_ms", "lat", "lng", "place", "kept_place"}

# --- a made-up world ---------------------------------------------------------------------------

_LAT0, _LNG0, _M_PER_DEGREE = 49.30, -123.14, 111_195.0


def spot(east: float, north: float = 0.0) -> tuple[float, float]:
    """A made-up position ``east`` and ``north`` metres from a made-up origin."""
    lat = _LAT0 + north / _M_PER_DEGREE
    return lat, _LNG0 + east / (_M_PER_DEGREE * math.cos(math.radians(lat)))


HOME = spot(0)


def u(number: int) -> str:
    """An event uid made of ``number``: 32 hex digits."""
    return f"{number:032x}"


HOME_AM, HOME_PM = u(1), u(2)
STOPS = ((u(11), spot(2000), "Corner Store"), (u(12), spot(4000), "Library"),
         (u(13), spot(6000), "Gas Bar"))


class Clock:
    """The minutes of one local day as UTC milliseconds: ``clock(8, 30)`` is half past eight."""

    def __init__(self, day: date) -> None:
        """The clock of ``day`` in Vancouver, whose midnights bound ``start`` and ``end``."""
        self.start, self.end = day_window(day, ZONE)

    def __call__(self, hour: int, minute: int = 0, second: int = 0) -> int:
        """The time ``hour:minute:second`` after the day's midnight."""
        return self.start + ((hour * 60 + minute) * 60 + second) * SECOND


class Fix(NamedTuple):
    """One fix of a made-up route: when, where (metres from the origin) and what iOS said."""

    at: int
    east: float
    north: float = 0.0
    accuracy: float = 5.0
    speed: float | None = None
    flags: int = 0

    @property
    def where(self) -> tuple[float, float]:
        """Its latitude and longitude."""
        return spot(self.east, self.north)


def run(
    start: int, seconds: Iterable[int], where: Callable[[int], tuple[float, float]],
    *, speed: float | None = None,
) -> list[Fix]:
    """Fixes ``seconds`` after ``start``, each where ``where(second)`` puts him."""
    return [Fix(start + t * SECOND, *where(t), speed=speed) for t in seconds]


def eastward(mps: float) -> Callable[[int], tuple[float, float]]:
    """A straight run east at ``mps`` from the origin."""
    return lambda t: (mps * t, 0.0)


def zigzag(
    start: int, count: int, *, every: int = 1, first: int = 0, speed: float | None = None,
) -> list[Fix]:
    """``count`` fixes east at 10 m/s, 40 m north and back at every other fix.

    No fix lies on the line between its neighbours, so none may be left out of the points. The
    first is ``first`` seconds after ``start`` and the next ones are ``every`` seconds apart.
    """
    seconds = [first + i * every for i in range(count)]
    return [
        Fix(start + t * SECOND, 10.0 * t, 40.0 * (i % 2), speed=speed)
        for i, t in enumerate(seconds)
    ]


def reach(fixes: list[Fix]) -> float:
    """Metres along the fixes in order, in a straight line between neighbours."""
    return sum(metres(a.where, b.where) for a, b in pairwise(fixes))


def frame(uid: str, kind: str, payload: dict[str, Any], at: int) -> dict[str, Any]:
    """A phone's event frame."""
    return {"event_uid": uid, "event_type": kind, "ts_epoch_ms": at, "payload": payload}


def visit(uid: str, arrived: int, departed: int | None, where: tuple[float, float],
          place: str | None = None) -> dict[str, Any]:
    """A ``phone.visit_observed`` frame: where he stayed, from ``arrived`` to ``departed``."""
    payload: dict[str, Any] = {
        "lat": where[0], "lng": where[1], "accuracy_m": 30.0, "arrived_at_ms": arrived,
    }
    if departed is not None:
        payload["departed_at_ms"] = departed
    if place is not None:
        payload["place"] = place
    return frame(uid, VISIT, payload, arrived if departed is None else departed)


def move(uid: str, activity: str, started: int, ended: int | None) -> dict[str, Any]:
    """A ``phone.motion_observed`` frame of high confidence."""
    payload: dict[str, Any] = {"activity": activity, "confidence": "high", "started_at_ms": started}
    if ended is not None:
        payload["ended_at_ms"] = ended
    return frame(uid, MOTION, payload, started if ended is None else ended)


def sample(uid: str, fix: Fix, place: str | None = None) -> dict[str, Any]:
    """A sampled ``phone.location_observed`` frame at ``fix``."""
    payload: dict[str, Any] = {"lat": fix.where[0], "lng": fix.where[1], "accuracy_m": fix.accuracy}
    if fix.speed is not None:
        payload["speed_mps"] = fix.speed
    if place is not None:
        payload["place"] = place
    return frame(uid, SAMPLED, payload, fix.at)


def beat(number: int, at: int, bpm: float, *, length: int = 0, uuid: int | None = None,
         ) -> dict[str, Any]:
    """A heart rate sample of the Watch that starts at ``at`` and lasts ``length``."""
    ident = number if uuid is None else uuid
    return frame(u(number), MEASURE, {
        "metric": "heart_rate", "start_ms": at, "end_ms": at + length, "value": bpm,
        "unit": "count/min", "sample_uuid": f"{ident:08x}-0000-4000-8000-{ident:012x}",
    }, at + length)


def slept(number: int, start: int, end: int) -> dict[str, Any]:
    """A core sleep stage of the Watch from ``start`` to ``end``."""
    return frame(u(number), HEALTH, {
        "metric": "sleep", "start_ms": start, "end_ms": end, "value": 3, "detail": "core",
    }, end)


def trip(
    clock: Clock, minutes: int, *, hour: int = 8, to: tuple[float, float] = HOME,
) -> list[dict[str, Any]]:
    """Home until ``hour``:00, one drive of ``minutes``, then three hours ``to`` (home again)."""
    left, back = clock(hour), clock(hour, minutes)
    return [
        visit(HOME_AM, clock(hour - 2), left, HOME),
        move(u(3), "automotive", left, back),
        visit(HOME_PM, back, clock(hour + 3, minutes), to),
    ]


class Lesson(NamedTuple):
    """The frames of a drive past three stops, and the times it is made of."""

    frames: list[dict[str, Any]]
    stops: list[tuple[int, int]]
    left: int
    back: int


def lesson(clock: Clock, middle: int) -> Lesson:
    """Home until 08:00, a drive past stops of 6, ``middle`` and 9 minutes, then home to noon."""
    frames = [visit(HOME_AM, clock(6), clock(8), HOME)]
    stops: list[tuple[int, int]] = []
    minute = 8 * 60
    for (uid, where, name), leg, length in zip(STOPS, (10, 9, 9), (6, middle, 9), strict=True):
        frames.append(move(u(int(uid, 16) + 100), "automotive", clock(0, minute),
                           clock(0, minute + leg)))
        minute += leg
        frames.append(visit(uid, clock(0, minute), clock(0, minute + length), where, name))
        stops.append((clock(0, minute), clock(0, minute + length)))
        minute += length
    frames.append(move(u(114), "automotive", clock(0, minute), clock(0, minute + 15)))
    minute += 15
    frames.append(visit(HOME_PM, clock(0, minute), clock(12), HOME))
    return Lesson(frames, stops, clock(8), clock(0, minute))


def assert_kept_within(fixes: list[Fix], points: list[list[Any]], metres_off: float = 5.0) -> None:
    """Each fix not among ``points`` lies within ``metres_off`` of where they put it at its time.

    The points are rounded to six decimals (about 0.1 m), which the allowance takes in.
    """
    times = [point[0] for point in points]
    assert times == sorted(set(times))
    assert times[0] == fixes[0].at
    assert times[-1] == fixes[-1].at
    for fix in fixes:
        if fix.at in times:
            continue
        after = bisect_left(times, fix.at)
        a, b = points[after - 1], points[after]
        share = (fix.at - a[0]) / (b[0] - a[0])
        lat, lng = a[1] + (b[1] - a[1]) * share, a[2] + (b[2] - a[2]) * share
        assert metres(fix.where, (lat, lng)) <= metres_off + 0.25, fix


# --- the app ---------------------------------------------------------------------------------


class Brain:
    """One app with the log, the day and the footprint wired, and phones ``iphone`` and ``ipad``."""

    def __init__(
        self, tmp_path: Path, *, footprint: bool = True, now: datetime = AFTERWARDS,
    ) -> None:
        """A brain over a fresh log and memory.db in ``tmp_path``, two phones paired, at ``now``.

        ``now`` is the footprint's clock, :attr:`clock`; the phones' frames are still taken at
        the real one, which trusts them within a week.
        """
        self.clock = FakeClock(now, zone=ZONE)
        self.log = tmp_path / "events.db"
        self.memory = tmp_path / "memory.db"
        open_memory_db(self.memory).close()
        self._uids = 5000
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T",
            broadcaster=InherentBroadcaster(),
            phone_events=BrainEvents(brain_log(self.log)),
            device_name=functools.partial(device_name_for_token, tmp_path),
            day_read=functools.partial(
                routes.serve_day, self.log, ZONE, self.memory, None,
            ),
            footprint_read=(
                functools.partial(
                    routes.serve_footprint, self.log, ZONE, self.memory,
                    clock=self.clock.time,
                ) if footprint else None
            ),
        ))
        self.key = local_key(tmp_path)
        require_local_key(
            app, functools.partial(local_key_matches, self.key), extra_hosts=[REMOTE],
            device_token_matches=functools.partial(device_token_matches, tmp_path),
        )
        self.tokens = {name: pair_device(tmp_path, name) for name in ("iphone", "ipad")}
        self.remote = TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000))
        self.loopback = TestClient(
            app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000),
        )

    def uid(self) -> str:
        """A fresh event uid, apart from the small numbers the tests name their stays by."""
        self._uids += 1
        return u(self._uids)

    def send(self, device: str, frames: list[dict[str, Any]]) -> None:
        """The frames through the phone's route under ``device``'s token, every one acked."""
        batch: list[dict[str, Any]] = []
        size = 0
        for one in frames:
            weight = len(json.dumps(one))
            if batch and (len(batch) == MAX_BATCH_FRAMES or size + weight > MAX_BATCH_BYTES // 2):
                self._post(device, batch)
                batch, size = [], 0
            batch.append(one)
            size += weight
        if batch:
            self._post(device, batch)

    def _post(self, device: str, batch: list[dict[str, Any]]) -> None:
        sent = self.remote.post(
            PHONE_EVENTS_PATH, json={"events": batch},
            headers={"Authorization": f"Bearer {self.tokens[device]}"},
        )
        assert sent.status_code == 200
        assert [ack["ok"] for ack in sent.json()["acks"]] == [True] * len(batch)

    def ride(self, fixes: list[Fix]) -> list[dict[str, Any]]:
        """Track frames that hold ``fixes``: at most 300 fixes and ten minutes to a frame."""
        frames: list[dict[str, Any]] = []
        group: list[Fix] = []
        for fix in fixes:
            if group and (len(group) == 300 or fix.at - group[0].at > 10 * MINUTE):
                frames.append(self._pack(group))
                group = []
            group.append(fix)
        if group:
            frames.append(self._pack(group))
        return frames

    def _pack(self, group: list[Fix]) -> dict[str, Any]:
        start = group[0].at
        rows = [
            [fix.at - start, *fix.where, fix.accuracy, 20.0, 3.0, fix.speed,
             None if fix.speed is None else 0.5, 90.0, 4.0, fix.flags]
            for fix in group
        ]
        payload = {"started_at_ms": start, "ended_at_ms": group[-1].at, "fixes": rows}
        return frame(self.uid(), TRACK, payload, group[-1].at)

    def samples(self, fixes: list[Fix], place: str | None = None) -> list[dict[str, Any]]:
        """Sampled location frames, one for each of ``fixes``."""
        return [sample(self.uid(), fix, place) for fix in fixes]

    def keep(self, name: str, where: tuple[float, float], role: str = "other") -> places.Place:
        """A place he keeps, written as the places tool writes it."""
        return places.save(
            self.memory, name=name, lat=where[0], lng=where[1], role=role, origin="user",
        ).place

    def get(self, path: str, **params: str) -> Any:  # noqa: ANN401 — the response as it came
        """``GET path`` as the Mac reads it, with the local key."""
        return self.loopback.get(
            path, params=params, headers={"Authorization": f"Bearer {self.key}"},
        )

    def footprint(self, day: date | None = None) -> dict[str, Any]:
        """The footprint of ``day`` (today with none), which must be a 200."""
        reply = self.get(FOOTPRINT, **({} if day is None else {"date": day.isoformat()}))
        assert reply.status_code == 200, reply.text
        body: dict[str, Any] = reply.json()
        return body

    def day(self, day: date) -> dict[str, Any]:
        """The same day as ``GET /inherent/day`` serves it."""
        reply = self.get("/inherent/day", date=day.isoformat())
        assert reply.status_code == 200, reply.text
        body: dict[str, Any] = reply.json()
        return body


def only(outings: list[dict[str, Any]]) -> dict[str, Any]:
    """The one outing of an answer."""
    assert len(outings) == 1, [o["id"] for o in outings]
    return outings[0]


# --- the outings -----------------------------------------------------------------------------


def test_one_drive_between_two_long_stays_with_three_short_stops_is_one_outing(
    tmp_path: Path,
) -> None:
    """A drive from home to home past stops of 6, 12 and 9 minutes: one outing, three stops."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    drive = lesson(clock, middle=12)
    brain.send("iphone", drive.frames)

    body = brain.footprint(DAY)

    assert set(body) == {"date", "tz", "start_ms", "end_ms", "now_ms", "stays", "outings"}
    assert (body["date"], body["tz"]) == (DAY.isoformat(), VANCOUVER)
    assert (body["start_ms"], body["end_ms"]) == (clock.start, clock.end)
    assert body["now_ms"] == brain.clock.time_ms()
    assert [stay["id"] for stay in body["stays"]] == [HOME_AM, HOME_PM]
    assert body["stays"][0] == {
        "id": HOME_AM, "start_ms": clock(6), "end_ms": clock(8), "lat": HOME[0], "lng": HOME[1],
        "place": None, "kept_place": None,
    }
    outing = only(body["outings"])
    assert set(outing) == OUTING_KEYS
    assert outing["id"] == f"outing-{drive.left}"
    assert (outing["start_ms"], outing["end_ms"]) == (drive.left, drive.back)
    assert (outing["from_stay"], outing["to_stay"]) == (HOME_AM, HOME_PM)
    assert outing["activity"] == "automotive"
    assert [stop["id"] for stop in outing["stops"]] == [STOPS[0][0], STOPS[1][0], STOPS[2][0]]
    assert outing["stops"][1] == {
        "id": STOPS[1][0], "start_ms": drive.stops[1][0], "end_ms": drive.stops[1][1],
        "lat": STOPS[1][1][0], "lng": STOPS[1][1][1], "place": "Library", "kept_place": None,
    }
    assert outing["moving_ms"] == 70 * MINUTE - (6 + 12 + 9) * MINUTE
    # no fix was sent: the route is empty and says nothing it does not know
    assert (outing["distance_m"], outing["max_speed_mps"]) == (0, None)
    assert (outing["gaps"], outing["points"], outing["heart_rate"]) == ([], [], None)
    assert brain.footprint(DAY - timedelta(days=1)) | {"now_ms": 0} == {
        "date": (DAY - timedelta(days=1)).isoformat(), "tz": VANCOUVER, "now_ms": 0,
        "start_ms": clock.start - 24 * HOUR, "end_ms": clock.start, "stays": [], "outings": [],
    }


def test_a_stay_of_25_minutes_splits_the_outing_in_two(tmp_path: Path) -> None:
    """A stop of 20 minutes or more is a long stay: the outing ends there and another begins."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    drive = lesson(clock, middle=25)
    brain.send("iphone", drive.frames)

    body = brain.footprint(DAY)

    assert [stay["id"] for stay in body["stays"]] == [HOME_AM, STOPS[1][0], HOME_PM]
    first, second = body["outings"]
    assert (first["from_stay"], first["to_stay"]) == (HOME_AM, STOPS[1][0])
    assert (second["from_stay"], second["to_stay"]) == (STOPS[1][0], HOME_PM)
    assert (first["start_ms"], first["end_ms"]) == (drive.left, drive.stops[1][0])
    assert (second["start_ms"], second["end_ms"]) == (drive.stops[1][1], drive.back)
    assert [stop["id"] for stop in first["stops"]] == [STOPS[0][0]]
    assert [stop["id"] for stop in second["stops"]] == [STOPS[2][0]]
    assert first["moving_ms"] == 25 * MINUTE - 6 * MINUTE
    assert second["moving_ms"] == (drive.back - drive.stops[1][1]) - 9 * MINUTE


def test_a_stay_of_8_minutes_in_a_kept_place_splits_it_too(tmp_path: Path) -> None:
    """The same drive is one outing until he keeps the library, then two: a kept place is long."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    drive = lesson(clock, middle=8)
    brain.send("iphone", drive.frames)

    before = brain.footprint(DAY)
    library = brain.keep("Library", STOPS[1][1])
    after = brain.footprint(DAY)

    assert len(only(before["outings"])["stops"]) == 3
    assert [stay["id"] for stay in before["stays"]] == [HOME_AM, HOME_PM]
    assert [stay["id"] for stay in after["stays"]] == [HOME_AM, STOPS[1][0], HOME_PM]
    assert after["stays"][1]["kept_place"] == {"id": library.id, "name": "Library", "role": "other"}
    first, second = after["outings"]
    assert (first["from_stay"], first["to_stay"]) == (HOME_AM, STOPS[1][0])
    assert (second["from_stay"], second["to_stay"]) == (STOPS[1][0], HOME_PM)
    assert [stop["id"] for stop in first["stops"] + second["stops"]] == [STOPS[0][0], STOPS[2][0]]


def test_a_stretch_is_an_outing_only_with_a_move_in_it_or_300_metres_between_its_stays(
    tmp_path: Path,
) -> None:
    """250 m on with no move is no outing; 700 m on is one; the same place after a walk is one."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    near, far = spot(250), spot(950)
    brain.send("iphone", [
        visit(u(21), clock(6), clock(7), HOME),
        visit(u(22), clock(7, 10), clock(8), near),
        visit(u(23), clock(8, 10), clock(9), far),
        move(u(25), "walking", clock(9), clock(9, 20)),
        visit(u(24), clock(9, 20), clock(10), far),
    ])

    body = brain.footprint(DAY)

    assert [stay["id"] for stay in body["stays"]] == [u(21), u(22), u(23), u(24)]
    assert [(o["from_stay"], o["to_stay"], o["activity"]) for o in body["outings"]] == [
        (u(22), u(23), None), (u(23), u(24), "walking"),
    ]
    assert [(o["start_ms"], o["end_ms"]) for o in body["outings"]] == [
        (clock(8), clock(8, 10)), (clock(9), clock(9, 20)),
    ]
    # no fix at all: the distance is the straight line from stay to stay
    assert [o["distance_m"] for o in body["outings"]] == [pytest.approx(700, abs=0.1), 0]


def test_a_drive_motion_never_named_is_an_outing_its_speed_names(tmp_path: Path) -> None:
    """No motion at all, only fixes moving at 36 km/h: a path (ADR 0316), driven, and an outing."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    away_id, away, name = STOPS[2]
    fixes = run(clock(8), range(0, 601, 30), eastward(10.0))
    brain.send("iphone", [
        visit(HOME_AM, clock(6), clock(8), HOME),
        *brain.samples(fixes),
        visit(away_id, clock(8, 10), clock(11), away, name),
    ])

    outing = only(brain.footprint(DAY)["outings"])

    assert (outing["from_stay"], outing["to_stay"]) == (HOME_AM, away_id)
    assert (outing["start_ms"], outing["end_ms"]) == (clock(8), clock(8, 10))
    assert outing["activity"] == "automotive"
    assert outing["distance_m"] == pytest.approx(6000, abs=1)


def test_the_activity_of_an_outing_is_the_move_that_lasted_longest_in_it(tmp_path: Path) -> None:
    """30 minutes of walking beat 10 of driving, and the other way round."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.send("iphone", [
        visit(u(21), clock(6), clock(7), HOME),
        move(u(31), "walking", clock(7), clock(7, 30)),
        move(u(32), "automotive", clock(7, 30), clock(7, 40)),
        visit(u(22), clock(7, 45), clock(8, 45), HOME),
        move(u(33), "automotive", clock(8, 45), clock(9, 15)),
        move(u(34), "walking", clock(9, 15), clock(9, 25)),
        visit(u(23), clock(9, 30), clock(10, 30), HOME),
    ])

    outings = brain.footprint(DAY)["outings"]

    assert [(o["from_stay"], o["to_stay"], o["activity"]) for o in outings] == [
        (u(21), u(22), "walking"), (u(22), u(23), "automotive"),
    ]


def test_before_the_first_long_stay_an_outing_starts_at_its_first_move(tmp_path: Path) -> None:
    """With no stay before it there is nothing to leave from: it begins when the first move does."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.send("iphone", [
        move(u(3), "automotive", clock(9, 30), clock(9, 50)),
        visit(HOME_PM, clock(10), clock(14), HOME),
    ])

    body = brain.footprint(DAY)

    outing = only(body["outings"])
    assert (outing["start_ms"], outing["end_ms"]) == (clock(9, 30), clock(10))
    assert (outing["from_stay"], outing["to_stay"]) == (None, HOME_PM)
    assert outing["activity"] == "automotive"
    assert [stay["id"] for stay in body["stays"]] == [HOME_PM]


def test_two_long_stays_that_touch_have_no_outing_between_them(tmp_path: Path) -> None:
    """A move across the join is still no time between the stays to draw."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.send("iphone", [
        visit(u(21), clock(6), clock(7), HOME),
        move(u(22), "walking", clock(6, 50), clock(7, 5)),
        visit(u(23), clock(7), clock(8), HOME),
    ])

    body = brain.footprint(DAY)

    assert [stay["id"] for stay in body["stays"]] == [u(21), u(23)]
    assert body["outings"] == []


def test_a_long_stay_alone_has_no_outing(tmp_path: Path) -> None:
    """Nothing before it moved and nothing came after: there is no outing to draw."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.send("iphone", [visit(HOME_AM, clock(6), clock(8), HOME)])

    body = brain.footprint(DAY)

    assert [stay["id"] for stay in body["stays"]] == [HOME_AM]
    assert body["outings"] == []


def test_no_date_is_today_and_an_outing_with_no_long_stay_after_it_is_still_going(
    tmp_path: Path,
) -> None:
    """At half past five the last long stay has ended, he is moving, and nothing has stopped him."""
    brain = Brain(tmp_path, now=EVENING)
    now = brain.clock.time_ms()
    left = now - 100 * MINUTE
    brain.send("iphone", [
        visit(HOME_AM, now - 5 * HOUR, left, HOME),
        move(u(3), "automotive", left, None),
        *brain.ride(zigzag(now - 30 * MINUTE, 61, every=10)),
    ])

    body = brain.footprint()

    assert (body["date"], body["tz"], body["now_ms"]) == (DAY.isoformat(), VANCOUVER, now)
    assert (body["start_ms"], body["end_ms"]) == day_window(DAY, ZONE)
    outing = only(body["outings"])
    assert (outing["start_ms"], outing["end_ms"]) == (left, None)
    assert (outing["from_stay"], outing["to_stay"]) == (HOME_AM, None)
    assert outing["activity"] == "automotive"
    assert outing["moving_ms"] == body["now_ms"] - left
    assert len(outing["points"]) == 61  # the route so far


def test_the_stay_he_is_still_at_ends_the_outing_however_short(tmp_path: Path) -> None:
    """Ten minutes after he got somewhere he is there, not still out: the outing ends at it."""
    brain = Brain(tmp_path, now=EVENING)
    now = brain.clock.time_ms()
    left, came = now - 60 * MINUTE, now - 10 * MINUTE
    here_id, where, name = STOPS[0]
    brain.send("iphone", [
        visit(HOME_AM, now - 5 * HOUR, left, HOME),
        move(u(3), "automotive", left, came),
        visit(here_id, came, None, where, name),
    ])

    body = brain.footprint()

    outing = only(body["outings"])
    assert (outing["start_ms"], outing["end_ms"]) == (left, came)
    assert (outing["from_stay"], outing["to_stay"]) == (HOME_AM, here_id)
    assert outing["stops"] == []
    assert [(stay["id"], stay["end_ms"]) for stay in body["stays"]][-1] == (here_id, None)


def test_an_outing_across_midnight_is_whole_on_both_days(tmp_path: Path) -> None:
    """Left at 23:20, home at 00:40: each day gives the whole outing and its own long stay."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    midnight = clock.end
    left, back = midnight - 40 * MINUTE, midnight + 40 * MINUTE
    fixes = run(left, range(0, 80 * 60 + 1, 10), eastward(20.0))
    brain.send("iphone", [
        visit(HOME_AM, midnight - 4 * HOUR, left, HOME),
        move(u(3), "automotive", left, back),
        visit(HOME_PM, back, back + 4 * HOUR, fixes[-1].where),
        *brain.ride(fixes),
    ])

    evening, morning = brain.footprint(DAY), brain.footprint(DAY + timedelta(days=1))

    assert evening["outings"] == morning["outings"]
    outing = only(evening["outings"])
    assert (outing["start_ms"], outing["end_ms"]) == (left, back)
    assert (outing["from_stay"], outing["to_stay"]) == (HOME_AM, HOME_PM)
    assert [p[0] for p in outing["points"]] == [left, back]  # a straight run: its two ends
    assert [stay["id"] for stay in evening["stays"]] == [HOME_AM]
    assert [stay["id"] for stay in morning["stays"]] == [HOME_PM]
    assert outing["distance_m"] == pytest.approx(reach(fixes), rel=1e-4)


# --- the stays are the day line's -------------------------------------------------------------


def test_the_stays_and_the_stops_are_the_day_lines_with_the_same_ids(tmp_path: Path) -> None:
    """A visit sent twice, a stay cut from fixes and what a visit leaves of it: the day's ids."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    cafe = spot(5000)
    brain.keep("Cafe", cafe)
    fixes = [Fix(clock(8, 25 + 5 * k), 5000.0 + k, float(k)) for k in range(14)]  # to 09:30
    arrival = {"lat": HOME[0], "lng": HOME[1], "accuracy_m": 30.0, "arrived_at_ms": clock(6)}
    brain.send("iphone", [
        frame(u(41), VISIT, arrival, clock(6)),  # first told alone, then with its departure
        visit(u(42), clock(6), clock(8), HOME),
        move(u(43), "automotive", clock(8), clock(8, 20)),
        visit(u(44), clock(8, 8), clock(8, 16), spot(2500), "Corner Store"),
        *brain.samples(fixes, place="Cafe"),  # a run of fixes at the cafe, no visit
        visit(u(45), clock(8, 50), clock(9), cafe, "Cafe"),  # a visit in the middle of the run
    ])

    body, day = brain.footprint(DAY), brain.day(DAY)

    on_the_line = {item["id"]: item for item in day["items"] if item["kind"] == "stay"}
    long_ones = [
        item["id"] for item in on_the_line.values()
        if item["end_ms"] is None or item["end_ms"] - item["start_ms"] >= 20 * MINUTE
        or "kept_place" in item
    ]
    assert [stay["id"] for stay in body["stays"]] == long_ones
    first_fix = u(5001)
    assert long_ones == [u(41), first_fix, u(45), f"{first_fix}@{clock(9)}"]
    outing = only(body["outings"])
    assert [stop["id"] for stop in outing["stops"]] == [u(44)]
    for stay in [*body["stays"], *outing["stops"]]:
        item = on_the_line[stay["id"]]
        assert set(stay) == STAY_KEYS
        assert (stay["start_ms"], stay["end_ms"]) == (item["start_ms"], item["end_ms"])
        assert (stay["lat"], stay["lng"]) == (item["lat"], item["lng"])
        assert stay["place"] == item.get("place")
        assert stay["kept_place"] == item.get("kept_place")
    assert body["stays"][1]["place"] == "Cafe"
    assert body["stays"][-1]["end_ms"] is None  # the run of fixes has not ended


# --- the route -------------------------------------------------------------------------------


def test_track_fixes_win_over_sampled_ones_and_sampled_ones_fill_where_the_track_has_none(
    tmp_path: Path,
) -> None:
    """The device with the most fixes, track and sampled together, is the route."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    track = zigzag(left, 121, first=20)  # a fix a second from 20 s to 140 s
    sampled = zigzag(left, 20, every=30, first=10)  # one every 30 s from 10 s to 580 s
    clash = Fix(left + 50 * SECOND, 500.0, 500.0)  # at the time of a track fix, 500 m from it
    twin = Fix(sampled[7].at, 3000.0, 300.0)  # at the time of another sampled fix
    elsewhere = [fix._replace(north=fix.north + 1000.0) for fix in zigzag(left, 130)]
    brain.send("iphone", [
        *trip(clock, 10), *brain.ride(track), *brain.samples([*sampled, clash, twin]),
    ])
    brain.send("ipad", brain.ride(elsewhere))  # more track fixes than the iPhone's, fewer in all

    outing = only(brain.footprint(DAY)["outings"])

    # the track, then the samples that are 30 s or more from it: not the one 10 s before it
    # began, nor those in it, nor the one 20 s after it ended
    seconds = [*range(20, 141), *range(190, 581, 30)]
    assert [point[0] for point in outing["points"]] == [left + t * SECOND for t in seconds]
    by_time = {point[0]: point for point in outing["points"]}
    assert by_time[left + 50 * SECOND][1:3] == pytest.approx(list(spot(500.0, 0.0)), abs=1e-6)
    assert by_time[left + 190 * SECOND][1:3] == pytest.approx(list(sampled[6].where), abs=1e-6)
    assert max(abs(point[1] - spot(0)[0]) for point in outing["points"]) < 0.0005  # not the iPad's


def test_a_gap_is_reported_and_its_straight_line_counts_in_the_distance(tmp_path: Path) -> None:
    """Five minutes and 1.5 km with no fix: no line is drawn and no speed is worked out across."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    before = run(left, range(241), eastward(10.0))  # 2,400 m in four minutes
    after = run(left, range(540, 781), lambda t: (3900.0 + 10.0 * (t - 540), 0.0))  # 1,500 m on
    brain.send("iphone", [*trip(clock, 20, to=after[-1].where), *brain.ride([*before, *after])])

    outing = only(brain.footprint(DAY)["outings"])

    start, end = left + 240 * SECOND, left + 540 * SECOND
    assert outing["gaps"] == [{"start_ms": start, "end_ms": end}]
    assert outing["distance_m"] == pytest.approx(2400 + 1500 + 2400, abs=0.5)
    times = [point[0] for point in outing["points"]]
    assert [start in times, end in times] == [True, True]
    assert not [t for t in times if start < t < end]
    speeds = {point[0]: point[3] for point in outing["points"]}
    assert speeds[start] == pytest.approx(10.0, abs=0.01)  # the line across the gap says 5
    assert speeds[end] == pytest.approx(10.0, abs=0.01)


def test_a_halt_or_a_brief_hole_in_the_fixes_is_no_gap(tmp_path: Path) -> None:
    """A gap is over 2 minutes and over 300 m: 150 s and 250 m, or 60 s and 600 m, is not."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    fixes = [
        *run(left, range(61), eastward(10.0)),
        *run(left, range(210, 271), lambda t: (850.0 + 10.0 * (t - 210), 0.0)),  # 150 s, 250 m on
        *run(left, range(330, 391), lambda t: (1450.0 + 10.0 * (t - 270), 0.0)),  # 60 s, 600 m on
    ]
    brain.send("iphone", [*trip(clock, 10, to=fixes[-1].where), *brain.ride(fixes)])

    outing = only(brain.footprint(DAY)["outings"])

    assert outing["gaps"] == []
    assert outing["distance_m"] == pytest.approx(2650, abs=0.5)
    assert [outing["points"][0][0], outing["points"][-1][0]] == [fixes[0].at, fixes[-1].at]


def test_a_gap_leaves_out_the_stops_in_it(tmp_path: Path) -> None:
    """GPS was off through two stops: only the time on the road with no fix is a gap.

    The first stop's last fix is at its start, and the next is 1.8 km on, three minutes after he
    left it: the gap is those three minutes. The second stop ends at the next fix, 400 m on: no
    time is left on the road, and there is no gap.
    """
    brain, clock = Brain(tmp_path), Clock(DAY)
    fixes = [
        *run(clock(8), range(0, 301, 30), eastward(10.0)),  # 3 km to the first stop at 08:05
        *run(clock(8, 16), range(0, 121, 30), lambda t: (4800.0 + 10.0 * t, 0.0)),  # to 6 km
        *run(clock(8, 25), range(0, 181, 30), lambda t: (6400.0 + 10.0 * t, 0.0)),  # to 8.2 km
    ]
    brain.send("iphone", [
        visit(HOME_AM, clock(6), clock(8), HOME),
        move(u(3), "automotive", clock(8), clock(8, 28)),
        visit(u(31), clock(8, 5), clock(8, 13), spot(3000)),
        visit(u(32), clock(8, 18), clock(8, 25), spot(6000)),
        visit(HOME_PM, clock(8, 28), clock(11), spot(8200)),
        *brain.samples(fixes),
    ])

    outing = only(brain.footprint(DAY)["outings"])

    assert [stop["id"] for stop in outing["stops"]] == [u(31), u(32)]
    assert outing["gaps"] == [{"start_ms": clock(8, 13), "end_ms": clock(8, 16)}]
    assert outing["distance_m"] == pytest.approx(8200, abs=0.5)


def test_the_route_runs_on_into_the_stay_after_while_his_fixes_still_close_in_on_it(
    tmp_path: Path,
) -> None:
    """His arrival is dated half a minute before he is there: the outing runs on to it.

    At the stay's start he is still 400 m out. The next fix is 100 m out, inside the stay's
    150 m: there he arrived. The one after it, nearer still, is him at the place, not on the way.
    """
    brain, clock = Brain(tmp_path), Clock(DAY)
    there = spot(6400)
    fixes = [*run(clock(8), range(0, 631, 30), eastward(10.0)), Fix(clock(8, 11), 6400.0)]
    brain.send("iphone", [
        visit(HOME_AM, clock(6), clock(8), HOME),
        move(u(3), "automotive", clock(8), clock(8, 10)),
        visit(u(31), clock(8, 10), clock(11), there),
        *brain.samples(fixes),
    ])

    body = brain.footprint(DAY)

    outing = only(body["outings"])
    assert (outing["start_ms"], outing["end_ms"]) == (clock(8), clock(8, 10, 30))
    stays = {stay["id"]: stay for stay in body["stays"]}
    assert outing["to_stay"] == u(31)
    assert stays[u(31)]["start_ms"] == clock(8, 10)  # the day line's stay, as iOS dated it
    assert outing["points"][-1][0] == clock(8, 10, 30)
    assert outing["distance_m"] == pytest.approx(6400, abs=0.5)
    assert outing["moving_ms"] == 10 * MINUTE + 30 * SECOND


def test_the_distance_runs_from_stay_to_stay_where_the_phone_woke_gps_late(tmp_path: Path) -> None:
    """GPS woke 500 m out of home and the fixes end 300 m short: both straight lines count."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    fixes = run(clock(8, 1), range(0, 251, 25), lambda t: (500.0 + 10.0 * t, 0.0))  # to 3 km
    brain.send("iphone", [
        visit(HOME_AM, clock(6), clock(8), HOME),
        move(u(3), "automotive", clock(8), clock(8, 6)),
        visit(u(31), clock(8, 6), clock(11), spot(3300)),
        *brain.samples(fixes),
    ])

    outing = only(brain.footprint(DAY)["outings"])

    assert [outing["points"][0][0], outing["points"][-1][0]] == [fixes[0].at, fixes[-1].at]
    assert outing["distance_m"] == pytest.approx(500 + 2500 + 300, abs=0.5)
    assert outing["gaps"] == []


def test_bad_and_simulated_fixes_are_left_out_of_the_route_and_the_distance(tmp_path: Path) -> None:
    """Worse than 50 m, or marked simulated, is out; 50 m and an accessory's fix are in."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    fixes = zigzag(left, 61)
    fixes[10] = fixes[10]._replace(accuracy=51.0)
    fixes[20] = fixes[20]._replace(accuracy=50.0)
    fixes[30] = fixes[30]._replace(flags=SIMULATED)
    fixes[40] = fixes[40]._replace(flags=2)  # produced by an accessory
    fixes[50] = fixes[50]._replace(accuracy=200.0)
    sampled = [Fix(left + 300 * SECOND, 3000.0, accuracy=80.0), Fix(left + 330 * SECOND, 3300.0)]
    brain.send("iphone", [
        *trip(clock, 10, to=sampled[1].where), *brain.ride(fixes), *brain.samples(sampled),
    ])

    outing = only(brain.footprint(DAY)["outings"])

    kept = [fix for i, fix in enumerate(fixes) if i not in (10, 30, 50)] + [sampled[1]]
    assert [point[0] for point in outing["points"]] == [fix.at for fix in kept]
    assert outing["distance_m"] == pytest.approx(reach(kept), rel=1e-4)


def test_a_track_row_the_phone_would_not_send_is_dealt_with_not_drawn(tmp_path: Path) -> None:
    """A fix with no accuracy is left out, and a speed below zero is no speed: it is worked out."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    fixes = zigzag(clock(8), 10, speed=10.0)
    brain.send("iphone", trip(clock, 10))
    (track,) = brain.ride(fixes)
    track["payload"]["fixes"][2][3] = None  # no accuracy
    track["payload"]["fixes"][6][6] = -1.0  # iOS's "no speed", which the phone sends as null
    with closing(open_event_log(brain.log)) as conn:
        emit_event(
            conn, type=TRACK, payload=track["payload"], ts_epoch_ms=track["ts_epoch_ms"],
            event_uid=track["event_uid"], ingestion_node="iphone",
        )

    outing = only(brain.footprint(DAY)["outings"])

    assert [point[0] for point in outing["points"]] == [f.at for i, f in enumerate(fixes) if i != 2]
    # the fix beside the bad one is worked out from its neighbours: 20 m in 2 s, as iOS says
    assert [point[3] for point in outing["points"]] == pytest.approx([10.0] * 9, abs=0.01)


def test_speeds_are_ten_second_means_and_the_top_speed_is_the_highest_of_them(
    tmp_path: Path,
) -> None:
    """The speed iOS gave, else distance over time, averaged over the 10 s around each point."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)

    def square(t: int) -> tuple[float, float]:
        """A minute east, a minute north, a minute east, always at 10 m/s."""
        if t <= 60:
            return 10.0 * t, 0.0
        if t <= 120:
            return 600.0, 10.0 * (t - 60)
        return 600.0 + 10.0 * (t - 120), 600.0

    # iOS says 8 m/s on the first leg, with a spike of 38 at 30 s, and nothing after it but 10 at
    # the second corner; the speed there is then what the positions say: 10 m/s on every leg
    ios = dict.fromkeys(range(61), 8.0) | {30: 38.0, 120: 10.0}
    fixes = [Fix(left + t * SECOND, *square(t), speed=ios.get(t)) for t in range(181)]
    brain.send("iphone", [*trip(clock, 10), *brain.ride(fixes)])

    def mean(t: int) -> float:
        near = [ios.get(s, 10.0) for s in range(max(0, t - 5), min(180, t + 5) + 1)]
        return sum(near) / len(near)

    outing = only(brain.footprint(DAY)["outings"])

    assert [(p[0] - left) // SECOND for p in outing["points"]] == [0, 60, 120, 180]  # the corners
    assert [p[3] for p in outing["points"]] == pytest.approx(
        [mean(0), mean(60), mean(120), mean(180)], abs=0.01,
    )
    assert mean(60) == pytest.approx(98 / 11)
    # the ten seconds around the spike, not the spike and not the 10 the other legs make
    assert outing["max_speed_mps"] == pytest.approx(max(mean(t) for t in range(181)), abs=0.01)
    assert outing["max_speed_mps"] == pytest.approx(118 / 11, abs=0.01)


def _steady(heading: str) -> Callable[[int], tuple[float, float]]:
    """A run at 10 m/s east or north, each fix up to 2 m to one side or the other of the line."""

    def where(t: int) -> tuple[float, float]:
        aside = float(t * 7 % 5 - 2)
        return (10.0 * t, aside) if heading == "east" else (aside, 10.0 * t)

    return where


@pytest.mark.parametrize("heading", ["east", "north"])
def test_a_straight_run_at_a_steady_pace_is_its_two_ends(tmp_path: Path, heading: str) -> None:
    """Fixes a couple of metres off the line are all within 5 m of it: only the ends are points.

    The distance is the line's, 3 km from 2 m off home: the jitter between the fixes, 88 m of it
    here, is no road.
    """
    brain, clock = Brain(tmp_path), Clock(DAY)
    fixes = run(clock(8), range(301), _steady(heading), speed=10.0)
    brain.send("iphone", [*trip(clock, 10, to=fixes[-1].where), *brain.ride(fixes)])

    outing = only(brain.footprint(DAY)["outings"])

    assert [point[0] for point in outing["points"]] == [fixes[0].at, fixes[-1].at]
    assert [point[3] for point in outing["points"]] == [10.0, 10.0]
    assert outing["max_speed_mps"] == 10.0
    assert outing["distance_m"] == pytest.approx(
        metres(HOME, fixes[0].where) + metres(fixes[0].where, fixes[-1].where), abs=0.1,
    )
    assert reach(fixes) - outing["distance_m"] > 50


def _bend(t: int) -> tuple[float, float]:
    """A bend of 2 km radius, round a quarter circle and more, at 10 m/s."""
    angle = 10.0 * t / 2000.0
    return 2000.0 * math.sin(angle), 2000.0 * (1.0 - math.cos(angle))


def _noisy(t: int) -> tuple[float, float]:
    """A straight run with the noise of a poor fix: up to 8 m either side, never the same twice."""
    return 10.0 * t, ((t * 2_654_435_761) % 2**32 / 2**32 - 0.5) * 16.0


def _noisy_north(t: int) -> tuple[float, float]:
    """The same noise on a run north, where the fixes stray east and west."""
    east, north = _noisy(t)
    return north, east


def _hairpin(t: int) -> tuple[float, float]:
    """Five minutes out, a U-turn, and five minutes back along the next lane."""
    return (10.0 * t, 0.0) if t <= 300 else (3000.0 - 10.0 * (t - 300), 12.0)


@pytest.mark.parametrize(
    "path",
    [_bend, _noisy, _noisy_north, _hairpin],
    ids=["bend", "noise", "noise-north", "hairpin"],
)
def test_no_fix_left_out_is_more_than_five_metres_from_the_points_around_it(
    tmp_path: Path, path: Callable[[int], tuple[float, float]],
) -> None:
    """Whatever the path, each dropped fix is within 5 m of where the kept points put it then."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    fixes = run(clock(8), range(601), path)
    brain.send("iphone", [*trip(clock, 15), *brain.ride(fixes)])

    outing = only(brain.footprint(DAY)["outings"])

    assert 2 < len(outing["points"]) < len(fixes)
    assert_kept_within(fixes, outing["points"])


def _stop_and_go(t: int) -> tuple[float, float]:
    """Five minutes along a straight road at 10 m/s, two minutes still, and on."""
    return 10.0 * min(t, 300) + 10.0 * max(0, t - 420), 0.0


def test_a_stop_on_a_straight_road_is_a_point_where_he_stopped_and_one_where_he_set_off(
    tmp_path: Path,
) -> None:
    """The road is straight but he was not on it all the while: the line must keep the time."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    fixes = run(left, range(601), _stop_and_go)
    brain.send("iphone", [*trip(clock, 15), *brain.ride(fixes)])

    outing = only(brain.footprint(DAY)["outings"])

    assert [point[0] for point in outing["points"]] == [
        left + seconds * SECOND for seconds in (0, 300, 420, 600)
    ]


def test_a_route_longer_than_twelve_hours_is_read_whole_across_the_edge_of_the_window(
    tmp_path: Path,
) -> None:
    """Fixes at the 12 hour mark and a millisecond either side of it are each there once."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    left = clock(8)
    seconds = range(0, 13 * 3600 + 1801, 30)  # to 13:30 after leaving
    edge = 12 * HOUR
    times = sorted({left + t * SECOND for t in seconds} | {left + edge - 1, left + edge + 1})
    fixes = [Fix(at, 1.0 * (at - left) / SECOND, 40.0 * (i % 2), speed=1.0)
             for i, at in enumerate(times)]
    brain.send("iphone", [
        visit(HOME_AM, clock(6), left, HOME),
        visit(HOME_PM, fixes[-1].at, fixes[-1].at + HOUR, fixes[-1].where),  # far: no move needed
        *brain.ride(fixes),
    ])

    outing = only(brain.footprint(DAY)["outings"])

    assert [point[0] for point in outing["points"]] == times
    assert {left + edge - 1, left + edge, left + edge + 1} <= set(times)
    assert outing["distance_m"] == pytest.approx(reach(fixes), rel=1e-4)
    assert outing["gaps"] == []


# --- heart rate ------------------------------------------------------------------------------


def test_heart_rate_is_the_samples_inside_the_outing_set_against_the_same_day_at_home(
    tmp_path: Path,
) -> None:
    """The samples that start in an outing, the day's mean at his kept home, and null with none."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.keep("Home", HOME, role="home")
    brain.keep("Library", STOPS[1][1])
    brain.send("iphone", [
        visit(HOME_AM, clock(0) - 2 * HOUR, clock(8), HOME),
        move(u(3), "automotive", clock(8), clock(8, 25)),
        visit(STOPS[1][0], clock(8, 25), clock(8, 34), STOPS[1][1], "Library"),
        move(u(4), "automotive", clock(8, 34), clock(8, 50)),
        visit(HOME_PM, clock(8, 50), clock(12), HOME),
        beat(51, clock(0) - HOUR, 150),  # the evening before, at home: another day
        beat(52, clock(6, 30), 60), beat(53, clock(7), 64), beat(54, clock(7, 30), 68),
        beat(55, clock(7, 59, 30), 70, length=MINUTE),  # starts at home, ends in the outing
        beat(58, clock(8, 20), 100), beat(56, clock(8, 10), 90), beat(57, clock(8, 15), 85.5),
        beat(59, clock(8, 20), 100, uuid=58),  # the same sample, sent again
        beat(60, clock(8, 30), 120),  # at the library: neither in an outing nor at home
        beat(61, clock(10), 62), beat(62, clock(11), 66),
    ])

    first, second = brain.footprint(DAY)["outings"]

    assert (first["to_stay"], second["from_stay"]) == (STOPS[1][0], STOPS[1][0])
    assert first["heart_rate"] == {
        "avg_bpm": 91.8, "max_bpm": 100, "home_avg_bpm": 65.0,
        "samples": [[clock(8, 10), 90], [clock(8, 15), 85.5], [clock(8, 20), 100]],
    }
    assert second["heart_rate"] is None


def test_the_heart_rate_at_home_leaves_out_the_hours_he_slept(tmp_path: Path) -> None:
    """Asleep his heart beats slower than at any time he could drive: only his awake hours count."""
    brain, clock = Brain(tmp_path), Clock(DAY)
    brain.keep("Home", HOME, role="home")
    brain.send("iphone", [
        visit(HOME_AM, clock(0) - 2 * HOUR, clock(8), HOME),
        move(u(3), "automotive", clock(8), clock(8, 30)),
        visit(HOME_PM, clock(8, 30), clock(12), HOME),
        slept(71, clock(0, 30), clock(6)),
        beat(52, clock(2), 50), beat(53, clock(4), 52),  # asleep at home
        beat(54, clock(6, 30), 70), beat(55, clock(7, 30), 74),  # up, still home
        beat(56, clock(8, 15), 90),  # driving
        beat(57, clock(10), 66),  # home again
    ])

    outing = only(brain.footprint(DAY)["outings"])

    assert (outing["heart_rate"]["avg_bpm"], outing["heart_rate"]["home_avg_bpm"]) == (90, 70.0)


# --- the route and who may ask ---------------------------------------------------------------


def test_a_bad_date_is_a_400_and_the_answer_names_the_day_asked(tmp_path: Path) -> None:
    """Every form but ``YYYY-MM-DD`` is refused with the reason, as the day line refuses it."""
    brain = Brain(tmp_path)
    for bad in ("", "tomorrow", "2026-13-01", "2026-02-30", "20261010", "2026-10-10T00:00",
                "\u0662\u0660\u0662\u0666-\u0661\u0660-\u0661\u0660"):  # Arabic-Indic digits
        reply = brain.get(FOOTPRINT, date=bad)
        assert reply.status_code == 400, bad
        assert reply.json() == {"detail": "date must be YYYY-MM-DD"}, bad
    reply = brain.get(FOOTPRINT, date="9999-12-31")
    assert (reply.status_code, reply.json()) == (400, {"detail": "date is out of range"})

    body = brain.footprint(date(2026, 10, 10))
    assert body["date"] == "2026-10-10"
    assert (body["start_ms"], body["end_ms"]) == day_window(date(2026, 10, 10), ZONE)
    assert (body["stays"], body["outings"]) == ([], [])


def test_the_footprint_opens_to_a_paired_device_and_the_local_key_and_no_one_else(
    tmp_path: Path,
) -> None:
    """A remote peer needs a paired device's token; on loopback the local key, as for the day."""
    brain = Brain(tmp_path)
    token = brain.tokens["iphone"]

    def bearer(value: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {value}"}

    assert brain.remote.get(FOOTPRINT).status_code == 401
    assert brain.remote.get(FOOTPRINT, headers=bearer("wrong")).status_code == 401
    assert brain.remote.get(FOOTPRINT, headers=bearer(brain.key)).status_code == 401
    assert brain.remote.get(FOOTPRINT, params={"date": "bad"}).status_code == 401
    assert brain.remote.get(FOOTPRINT, headers=bearer(token)).status_code == 200
    assert brain.loopback.get(FOOTPRINT).status_code == 401
    assert brain.loopback.get(FOOTPRINT, headers=bearer(token)).status_code == 401
    assert brain.loopback.get(FOOTPRINT, headers=bearer(brain.key)).status_code == 200


def test_without_a_footprint_reader_there_is_no_route(tmp_path: Path) -> None:
    """A daemon that does not wire it answers 404 to every credential."""
    brain = Brain(tmp_path, footprint=False)

    assert brain.get(FOOTPRINT).status_code == 404
