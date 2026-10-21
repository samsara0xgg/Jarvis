"""ADR 0234 — the places he keeps: the table, the counted facts, the tools and what reads them.

Real memory.db and event log in a tmp dir, the real default registry and dispatcher, the real
routes through ``TestClient``, the real ``Whereabouts``, ``render_context``, day line and ledger.
Nothing here edits a stored row to set a scene: places come from the tools, the page or the
settings migration, and stays are the phone's own ``phone.visit_observed`` rows.

The month is fabricated around (48.0000, -123.0000), on made-up streets, and read on Sunday
2026-10-11 at noon in Vancouver. A ``Haunt`` is a spot with the days (counted back from today) he
was there, so the facts the program must count are plain arithmetic over that table.
"""

from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.execution import place_tools, transit_tool
from jarvis.execution.tools import build_default_registry
from jarvis.runtime import _locate_places, _places_block, _transit_places
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.ledger import LedgerContext, LedgerSettings
from jarvis.runtime.memory_page import MemoryPage
from jarvis.runtime.rides import Rides
from jarvis.runtime.whereabouts import ASK_PLACE_LINE, Whereabouts
from jarvis.shared import CallerPrincipal
from jarvis.state import places
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import open_memory_db, remember_fact, render_context
from jarvis.state.memory_page import Conflict
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.support.core_memory import consolidation_input, consolidator, core_schedule, seed_days
from tests.support.day_summaries import TODAY as CORE_TODAY
from tests.support.day_summaries import day_ago
from tests.support.ledger import NOW as LEDGER_NOW
from tests.support.ledger import ZONE as LEDGER_ZONE
from tests.support.ledger import ledger_world
from tests.support.places import NOW, NOW_MS, TODAY, ZONE, ms_days_ago, stay
from tests.support.tool_world import ToolWorld, tool_request
from tests.support.transit import FAKE_KEY, sent_request, serve_routes

if TYPE_CHECKING:
    from collections.abc import Iterator

MINUTE = 60_000


class Haunt(NamedTuple):
    """A spot, the days he was there (days ago), how long he stayed and what the phone calls it."""

    key: str
    lat: float
    lng: float
    labels: tuple[str, ...]
    days: tuple[int, ...]
    minutes: int
    hour: int
    wander: tuple[tuple[float, float], ...] = ((0.0, 0.0),)

    def rows(self) -> Iterator[tuple[str, float, float, int, int, str]]:
        """``(uid, lat, lng, arrived, departed, label)`` of every stay, oldest first."""
        for index, days_ago in enumerate(sorted(self.days, reverse=True)):
            dlat, dlng = self.wander[index % len(self.wander)]
            arrived = ms_days_ago(days_ago, self.hour)
            yield (
                f"{self.key}-{index}", self.lat + dlat, self.lng + dlng, arrived,
                arrived + self.minutes * MINUTE, self.labels[index % len(self.labels)],
            )

    def centre(self) -> tuple[float, float]:
        """The mean of his stays: the spot's running centre once they are all in."""
        rows = list(self.rows())
        return sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows)

    @property
    def spot_id(self) -> str:
        """The id a listing gives the spot."""
        lat, lng = self.centre()
        return f"spot:{lat:.4f},{lng:.4f}"


GYM_DAYS = (2, 4, 7, 9, 11, 14, 16, 18, 21, 23, 25, 28, 30, 35, 42, 49, 56)
HOME = Haunt("home", 48.0000, -123.0000, ("7 Dwelling Rd",), tuple(range(1, 41)), 240, 19)
GYM = Haunt(
    "gym", 48.0050, -123.0000, ("Iron Works Gym",), GYM_DAYS, 75, 7,
    ((0.0, 0.0), (0.0003, 0.0), (-0.0003, 0.0004), (0.0, -0.0004)),
)
CAFE = Haunt(
    "cafe", 48.0000, -123.0100, ("莲香小馆", "莲香小馆", "莲香小馆", "Lotus Rd 12"),
    (3, 5, 8, 12, 19, 26, 33, 61), 40, 12,
)
SHOP = Haunt("shop", 48.0100, -123.0100, ("Corner Shop",), (5, 40), 10, 17)
MILL = Haunt("mill", 48.0200, -123.0100, ("Old Mill",), (50, 60), 30, 15)
NORTH = Haunt(
    "north", 48.0300, -123.0000, ("North Rd",), (1, 6, 13), 20, 9,
    ((0.0, 0.0), (0.0002, -0.0002), (-0.0002, 0.0002)),
)
NORTH2 = Haunt("north2", 48.0313, -123.0000, ("North Rd 2",), (2, 9, 16), 20, 14)  # 145 m on
STOPS = tuple(  # twelve places he went once in the last four weeks, 222 m apart
    Haunt(f"stop{i}", 48.1000 + 0.002 * i, -123.2000, (f"Stop {i}",), (10 + i % 6,), 15, 5 + i)
    for i in range(12)
)
HAUNTS = (HOME, GYM, CAFE, SHOP, MILL, NORTH, NORTH2, *STOPS)


def seed(conn: sqlite3.Connection, haunts: tuple[Haunt, ...] = HAUNTS) -> None:
    """Every haunt's stays into the log, each reported with its departure."""
    for haunt in haunts:
        for uid, lat, lng, arrived, departed, label in haunt.rows():
            stay(conn, uid, lat, lng, arrived, departed, label)


def tally(conn: sqlite3.Connection, held: list[places.Place], now_ms: int = NOW_MS) -> places.Tally:
    """The program's count as of ``now_ms``."""
    return places.tally(places.read_stays(conn, now_ms, ZONE), held, now_ms, ZONE)


def spot_for(counted: places.Tally, haunt: Haunt) -> places.Spot:
    """The listed spot nearest a haunt's centre."""
    return places.spot_at(counted.spots, haunt.spot_id)


def expected(haunt: Haunt) -> places.Facts:
    """The facts, as plain arithmetic over the table, counted at noon today."""
    return places.Facts(
        sum(d <= 27 for d in haunt.days),
        sum(d <= 83 for d in haunt.days),
        TODAY - timedelta(days=min(haunt.days)),
        haunt.minutes,
        max(set(haunt.labels), key=haunt.labels.count),
    )


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tools read the clock from one function; pin it to noon on the 11th."""
    monkeypatch.setattr(place_tools, "_now_ms", lambda: NOW_MS)


class World:
    """One tmp runtime: memory.db, the log, the default registry and its dispatcher."""

    def __init__(self, tmp_path: Path) -> None:
        """Wire the real registry onto a store under ``tmp_path``."""
        self.tmp = tmp_path
        self.db = tmp_path / "memory.db"
        with closing(open_memory_db(self.db)):
            pass
        self.tools = build_default_registry(
            memory_db_path=self.db, local_zone=ZONE,
        ).get_definitions()
        self.fx = ToolWorld(tmp_path, tools=self.tools)
        self.conn = self.fx.conn
        self.log = self.fx.paths.event_log
        self.count = 0
        self.fixes = 0

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a tool the way the model's call does and decode what it persisted."""
        self.count += 1
        result = self.fx.dispatch(tool_request(name, f"places{self.count}", arguments=args))
        assert result.tool_output is not None
        return dict(json.loads(result.tool_output))

    def rows(self, table: str = "places") -> list[sqlite3.Row]:
        """Every row of a memory.db table, oldest first."""
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608

    def kept(self) -> list[places.Place]:
        """The view now."""
        return places.read_kept(self.db)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """The fabricated month in the log, nothing kept yet."""
    found = World(tmp_path)
    seed(found.conn)
    try:
        yield found
    finally:
        found.fx.close()


# --- the facts ------------------------------------------------------------------------------


def test_the_month_clusters_into_spots_with_the_facts_the_table_gives(world: World) -> None:
    """Every haunt is one spot with its days, last visit, usual stay and the phone's usual label."""
    counted = tally(world.conn, [])
    assert counted.kept == {}
    assert len(counted.spots) == len(HAUNTS)  # a wandering haunt is one spot; 145 m apart, two
    for haunt in HAUNTS:
        spot = spot_for(counted, haunt)
        assert spot.facts == expected(haunt), haunt.key
        lat, lng = haunt.centre()
        assert (round(spot.lat, 9), round(spot.lng, 9)) == (round(lat, 9), round(lng, 9))
        assert spot.id == haunt.spot_id
    # Counted again from the same rows it is the same count, in the same order.
    assert tally(world.conn, []) == counted


def test_a_spot_older_than_twelve_weeks_is_not_counted(world: World) -> None:
    """Stays that end before the 84-day window are not facts: no spot, no days."""
    old = Haunt("old", 48.0400, -123.0000, ("Closed Bakery",), (84, 90), 30, 10)
    seed(world.conn, (old,))
    assert not [s for s in tally(world.conn, []).spots if s.facts.label == "Closed Bakery"]
    edge = Haunt("edge", 48.0420, -123.0000, ("Last Day",), (83,), 30, 10)
    seed(world.conn, (edge,))
    (found,) = [s for s in tally(world.conn, []).spots if s.facts.label == "Last Day"]
    assert (found.facts.days_28, found.facts.days_84) == (0, 1)


def test_a_stay_over_midnight_counts_for_both_days_and_an_open_one_runs_to_now(
    world: World,
) -> None:
    """Days are the local days a stay overlaps; a stay with no departure is measured to now."""
    far = (48.0600, -123.0000)
    # 22:00 to 06:00
    stay(world.conn, "night", *far, ms_days_ago(3, 22), ms_days_ago(2, 6), "Friend's Place")
    stay(world.conn, "open", far[0], far[1] + 0.0001, ms_days_ago(0, 9), None, "Friend's Place")
    (found,) = [s for s in tally(world.conn, []).spots if s.facts.label == "Friend's Place"]
    assert found.facts.days_28 == 3
    assert found.facts.last_visit == TODAY
    assert found.facts.median_minutes == 8 * 60  # the open stay has no length yet


def test_the_smallest_circle_wins_and_a_kept_place_leaves_the_spots(world: World) -> None:
    """A stay in two kept circles is the smaller's; both leave the spots; a forget returns them."""
    campus = places.Place("p1", "campus", "other", 48.0500, -123.0000, 600, None, "user", "t", "t")
    library = places.Place("p2", "library", "other", 48.0510, -123.0000, 80, None, "user", "t", "t")
    elsewhere = (48.0650, -123.0000)
    for uid, lat, lng, day in (
        ("c", 48.0500, -123.0000, 1),  # 111 m from the library: the campus only
        ("l", 48.0509, -123.0000, 2),  # 10 m from the library, inside the campus too
        ("e", *elsewhere, 3),
    ):
        stay(world.conn, uid, lat, lng, ms_days_ago(day, 10), ms_days_ago(day, 11), f"Label {uid}")
    held = [campus, library]
    assert places.containing(held, 48.0509, -123.0000) == library
    assert places.containing(held, 48.0500, -123.0000) == campus
    assert places.containing(held, 48.0700, -123.0000) is None
    assert places.containing([library, campus], 48.0509, -123.0000) == library
    counted = tally(world.conn, held)
    assert (counted.kept["p1"].days_84, counted.kept["p2"].days_84) == (1, 1)
    assert counted.kept["p2"].label == "Label l"
    assert not [s for s in counted.spots if s.facts.label in {"Label c", "Label l"}]
    assert [s.facts.label for s in counted.spots if s.facts.label == "Label e"] == ["Label e"]
    # Forgetting the campus gives its stay back to the spots; the library keeps its own.
    back = tally(world.conn, [library])
    assert [s for s in back.spots if s.facts.label == "Label c"]
    assert back.kept["p2"] == counted.kept["p2"]


def test_equal_circles_go_to_the_older_place() -> None:
    """Two circles of one size that hold a point: the one kept first."""
    first = places.Place("a", "first", "other", 48.0, -123.0, 100, None, "user", "t", "t")
    second = places.Place("b", "second", "other", 48.0004, -123.0, 100, None, "user", "t", "t")
    assert places.containing([first, second], 48.0002, -123.0) == first
    assert places.containing([second, first], 48.0002, -123.0) == second
    assert places.containing([replace_address_only(first)], 48.0, -123.0) is None


def replace_address_only(one: places.Place) -> places.Place:
    """The same place with only an address: it holds no stay."""
    return places.Place(
        one.id, one.name, one.role, None, None, one.radius_m, "1 Example St", one.origin,
        one.created_at, one.updated_at,
    )


# --- the tools ------------------------------------------------------------------------------


def test_the_tools_are_registered_for_her_and_the_save_description_is_the_approved_text(
    world: World,
) -> None:
    """Three tools, her only, L1; `places` reads and the other two write."""
    by_name = {t.name: t for t in world.tools}
    for name in ("places", "save_place", "forget_place"):
        assert by_name[name].allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
        assert by_name[name].risk_level == "L1"
    assert by_name["places"].read_only
    assert not by_name["save_place"].read_only
    assert not by_name["forget_place"].read_only
    assert by_name["save_place"].description == (
        "Keep a place in his long-term memory under a name, or rename one he keeps. Call it when "
        'he names a place ("this is my home", "这里是健身房"), tells you he goes somewhere '
        "often, or answers your question about a spot. You may also keep a place on your own "
        "judgement when `places` shows he goes there regularly and it clearly matters to him, "
        "but only under a name he has used: never invent a name, and never use the phone's "
        'label as one. `where` is "here" (where his phone is now), or a spot id or a place id '
        "from `places`. Places he goes to only now and then are not kept; they stay counted."
    )
    assert by_name["forget_place"].description == (
        "Remove a kept place when he asks you to forget it or says it is no longer his (he "
        "moved out, quit the gym). Its visits stay counted."
    )
    assert by_name["save_place"].input_schema["required"] == ["name", "where"]


def test_places_lists_the_kept_and_the_ten_most_visited_spots_and_answers_a_query(
    world: World,
) -> None:
    """Top ten unkept spots by days in 28 with their facts; a query reaches any label."""
    listing = world.call("places", {})
    assert listing["kept"] == []
    spots = listing["spots"]
    assert len(spots) == 10
    first_six = [HOME, GYM, CAFE, NORTH, NORTH2, SHOP]
    assert [s["id"] for s in spots[:6]] == [h.spot_id for h in first_six]
    assert [s["days_28"] for s in spots] == sorted((s["days_28"] for s in spots), reverse=True)
    assert all(s["days_28"] >= 1 for s in spots)
    gym = spots[1]
    assert gym == {
        "id": GYM.spot_id, "days_28": 11, "days_84": 17, "last_visit": "2026-10-09",
        "median_stay_min": 75, "phone_label": "Iron Works Gym",
    }
    # "How often do I go to 莲香": a query finds it by the phone's label.
    asked = world.call("places", {"query": "莲香"})
    assert [s["id"] for s in asked["spots"]] == [CAFE.spot_id]
    assert asked["spots"][0]["days_28"] == 6
    assert asked["spots"][0]["days_84"] == 8
    # A spot he has not been to in four weeks is listed only when asked for.
    assert not [s for s in spots if s["phone_label"] == "Old Mill"]
    mill = world.call("places", {"query": "old MILL"})["spots"]
    assert [(s["days_28"], s["days_84"], s["last_visit"]) for s in mill] == [(0, 2, "2026-08-22")]


def test_here_needs_a_fresh_fix_then_keeps_a_place_with_a_version(world: World) -> None:
    """No fix, or one older than 30 minutes, is a clear error; a fresh one keeps the place."""
    args = {"name": "gym", "where": "here"}
    assert world.call("save_place", args)["code"] == "location_unavailable"
    stay(world.conn, "stale", 48.0050, -123.0001, NOW_MS - 40 * MINUTE)
    refused = world.call("save_place", args)
    assert refused["code"] == "location_unavailable"
    assert "30 minutes" in refused["error"]
    assert world.rows() == []
    assert world.rows("place_changes") == []
    stay(world.conn, "fresh", 48.0050, -123.0001, NOW_MS - 5 * MINUTE)
    kept = world.call("save_place", args)
    assert kept["unchanged"] is False
    assert {k: kept["kept"][k] for k in ("name", "role", "radius_m", "has_centre", "origin")} == {
        "name": "gym", "role": "other", "radius_m": 120, "has_centre": True, "origin": "model",
    }
    (row,) = world.rows()
    assert (row["lat"], row["lng"], row["address"]) == (48.0050, -123.0001, None)
    (change,) = world.rows("place_changes")
    assert (change["op"], change["origin"], change["place_id"]) == ("save", "model", row["id"])
    assert change["before"] is None
    assert json.loads(change["after"])["name"] == "gym"
    # Saying it again is a no-op, not a second version.
    assert world.call("save_place", args)["unchanged"] is True
    assert len(world.rows("place_changes")) == 1
    # The tool counts its visits: the 11 days in four weeks, and today's arrival.
    listed = world.call("places", {})["kept"]
    assert [(p["name"], p["days_28"], p["last_visit"]) for p in listed] == [
        ("gym", 12, TODAY.isoformat()),
    ]


def test_a_spot_id_keeps_a_place_even_after_a_new_stay_moves_its_centre(world: World) -> None:
    """The id is the centre rounded; it resolves to the nearest spot within 100 m."""
    spot_id = next(
        s["id"] for s in world.call("places", {"query": "莲香"})["spots"]
    )
    stay(world.conn, "new", 48.0008, -123.0100, ms_days_ago(0, 9), ms_days_ago(0, 10), "莲香小馆")
    moved = tally(world.conn, [])
    assert spot_for(moved, CAFE).id != spot_id  # the spot has no stable id
    kept = world.call(
        "save_place", {"name": "莲香", "where": spot_id, "role": "other", "radius_m": 150},
    )
    assert kept["kept"]["radius_m"] == 150
    (place,) = world.kept()
    assert place.name == "莲香"
    assert place.lat is not None
    assert abs(place.lat - CAFE.centre()[0]) < 0.0002
    gone = world.call("save_place", {"name": "x", "where": "spot:49.0000,-123.0000"})
    assert gone["code"] == "not_found"
    assert world.call("save_place", {"name": "x", "where": "somewhere"})["code"] == "not_found"


def test_rename_forget_and_the_refusals(world: World) -> None:
    """Rename by place id, the clashes and bad inputs refused, forget leaves the visits counted."""
    stay(world.conn, "fresh", 48.0050, -123.0001, NOW_MS - 5 * MINUTE)
    world.call("save_place", {"name": "gym", "where": "here"})
    (gym,) = world.kept()
    renamed = world.call("save_place", {"name": "Iron Works", "where": gym.id, "role": "other"})
    assert renamed["kept"]["name"] == "Iron Works"
    rename = world.rows("place_changes")[-1]
    assert rename["op"] == "rename"
    assert (json.loads(rename["before"])["name"], json.loads(rename["after"])["name"]) == (
        "gym", "Iron Works",
    )
    # Here is inside the circle he already keeps: it names the place and how to rename it.
    inside = world.call("save_place", {"name": "Other Name", "where": "here"})
    assert inside["code"] == "invalid_arguments"
    assert "Iron Works" in inside["error"]
    assert gym.id in inside["error"]
    # A name kept elsewhere, a radius out of range and a role that is not one.
    stay(world.conn, "far", 48.0200, -123.0000, NOW_MS - 2 * MINUTE)
    other = world.call("save_place", {"name": "iron works", "where": "here"})
    assert other["code"] == "invalid_arguments"
    assert world.call("save_place", {"name": "Dojo", "where": "here", "radius_m": 10})[
        "code"
    ] == "invalid_arguments"
    assert world.call("save_place", {"name": "Dojo", "where": "here", "role": "castle"})[
        "code"
    ] == "invalid_arguments"
    assert world.call("save_place", {"name": "", "where": "here"})["code"] == "invalid_arguments"
    assert world.call("save_place", {"name": "Dojo", "where": "place:nope"})["code"] == "not_found"
    assert len(world.rows("place_changes")) == 2
    # Forget: the place goes, the change keeps what it was, the visits are spots again.
    assert world.call("forget_place", {"place": "iron WORKS"}) == {"forgotten": "Iron Works"}
    assert world.kept() == []
    forgot = world.rows("place_changes")[-1]
    assert (forgot["op"], forgot["after"]) == ("forget", None)
    assert json.loads(forgot["before"])["name"] == "Iron Works"
    assert world.call("forget_place", {"place": gym.id})["code"] == "not_found"
    after = world.call("places", {})
    assert after["kept"] == []
    assert after["spots"][1]["days_28"] == 12


def test_a_place_that_came_as_an_address_gets_its_centre_when_he_is_there(world: World) -> None:
    """School kept as an address only routes; naming it on the spot gives it a circle."""
    places.migrate_settings(world.db, {"school": "1 Example Campus Way"})
    (school,) = world.kept()
    assert (school.lat, school.address, school.role) == (None, "1 Example Campus Way", "school")
    stay(world.conn, "fresh", 48.0700, -123.0000, NOW_MS - 3 * MINUTE)
    kept = world.call("save_place", {"name": "school", "where": "here"})
    assert kept["unchanged"] is False
    (school,) = world.kept()
    assert (school.lat, school.lng) == (48.0700, -123.0000)
    assert (school.address, school.role, school.origin) == (
        "1 Example Campus Way", "school", "settings",
    )
    assert [r["op"] for r in world.rows("place_changes")] == ["save", "save"]


# --- located from the address (ADR 0237) ----------------------------------------------------

ADDRESS = "1 Example Campus Way"


def found_at(lat: float, lng: float, precision: str = "ROOFTOP") -> dict[str, Any]:
    """One Geocoding result at ``lat, lng``."""
    return {"geometry": {"location": {"lat": lat, "lng": lng}, "location_type": precision}}


def google(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any] | Exception,
) -> list[dict[str, str]]:
    """Serve ``answer`` to every Geocoding request; returns each request's query parameters."""
    asked: list[dict[str, str]] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        url = urllib.parse.urlsplit(request.full_url)
        assert (url.scheme, url.netloc, url.path) == (
            "https", "maps.googleapis.com", "/maps/api/geocode/json",
        )
        assert timeout > 0
        asked.append(dict(urllib.parse.parse_qsl(url.query)))
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return asked


def test_a_place_kept_as_an_address_is_located_once_and_his_stays_there_become_its(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One request per place; the centre is a versioned change; undoing it does not ask again."""
    places.migrate_settings(world.db, {"school": ADDRESS})
    assert world.call("places", {})["kept"][0]["days_28"] == 0
    asked = google(monkeypatch, {"status": "OK", "results": [found_at(GYM.lat, GYM.lng)]})
    _locate_places(world.db, FAKE_KEY)
    assert asked == [{"address": ADDRESS, "key": FAKE_KEY}]
    (school,) = world.kept()
    assert (school.lat, school.lng, school.address) == (GYM.lat, GYM.lng, ADDRESS)
    assert (school.name, school.role, school.origin) == ("school", "school", "settings")
    assert [(r["op"], r["origin"]) for r in world.rows("place_changes")] == [
        ("save", "settings"), ("locate", "geocoder"),
    ]
    assert places.home_school(world.db) == {"school": f"{GYM.lat},{GYM.lng}"}
    kept = world.call("places", {})["kept"]
    assert [(k["name"], k["has_centre"], k["days_28"], k["days_84"]) for k in kept] == [
        ("school", True, 11, 17),
    ]
    _locate_places(world.db, FAKE_KEY)  # located: nothing left to ask about
    assert len(asked) == 1
    located = world.rows("place_changes")[-1]["id"]
    places.undo(world.db, located, origin="page")
    assert world.kept()[0].lat is None
    _locate_places(world.db, FAKE_KEY)  # once per place: taken back, it waits for him there
    assert len(asked) == 1


@pytest.mark.parametrize(
    ("answer", "said"),
    [
        (
            {"status": "REQUEST_DENIED", "error_message": "This API is not activated."},
            "status REQUEST_DENIED: This API is not activated.",
        ),
        ({"status": "ZERO_RESULTS", "results": []}, "status ZERO_RESULTS"),
        ({"status": "OK", "results": [found_at(48.07, -123.0)] * 2}, "2 results"),
        (
            {"status": "OK", "results": [found_at(48.07, -123.0, "APPROXIMATE")]},
            "only an approximate match",
        ),
        (
            urllib.error.HTTPError(
                f"https://maps.googleapis.com/maps/api/geocode/json?key={FAKE_KEY}", 403,
                "Forbidden", {}, None,  # type: ignore[arg-type]
            ),
            "HTTP 403",
        ),
        (urllib.error.URLError(f"no route to ?key={FAKE_KEY}"), "URLError"),
    ],
    ids=["denied", "nothing", "two", "approximate", "http", "network"],
)
def test_an_address_google_cannot_place_stays_an_address_and_is_asked_again_next_boot(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    answer: dict[str, Any] | Exception, said: str,
) -> None:
    """Nothing is written; the warning names the place and why, never the address or the key."""
    places.migrate_settings(world.db, {"school": ADDRESS})
    asked = google(monkeypatch, answer)
    with caplog.at_level("INFO", logger="jarvis.runtime"):
        _locate_places(world.db, FAKE_KEY)
    assert [r.getMessage() for r in caplog.records] == [f"place 'school' not located: {said}"]
    assert FAKE_KEY not in caplog.text
    assert ADDRESS not in caplog.text
    assert world.kept()[0].lat is None
    assert [r["op"] for r in world.rows("place_changes")] == ["save"]
    _locate_places(world.db, FAKE_KEY)
    assert len(asked) == 2


def test_locating_leaves_a_centre_he_gave_first_and_will_not_land_inside_a_kept_circle(
    world: World,
) -> None:
    """He named it on the spot before the answer came; or the point is a kept place's already."""
    places.migrate_settings(world.db, {"school": ADDRESS})
    (school,) = world.kept()
    places.save(world.db, name="school", lat=48.07, lng=-123.0, origin="user")
    assert places.unlocated(world.db) == []
    unchanged = places.locate(world.db, school.id, lat=48.5, lng=-123.5)
    assert unchanged.change_id is None
    assert (unchanged.place.lat, unchanged.place.lng) == (48.07, -123.0)
    places.forget(world.db, school.id, origin="user")
    with pytest.raises(LookupError):
        places.locate(world.db, school.id, lat=48.5, lng=-123.5)

    fresh = World(world.tmp / "again")
    try:
        places.migrate_settings(fresh.db, {"school": ADDRESS})
        places.save(fresh.db, name="gym", lat=GYM.lat, lng=GYM.lng, origin="user")
        (school,) = (p for p in fresh.kept() if p.role == "school")
        with pytest.raises(ValueError, match="inside “gym”"):
            places.locate(fresh.db, school.id, lat=GYM.lat, lng=GYM.lng + 0.0001)
        assert [r["op"] for r in fresh.rows("place_changes")] == ["save", "save"]
    finally:
        fresh.fx.close()


# --- versions and undo -----------------------------------------------------------------------


def test_every_write_is_one_transaction_of_a_change_and_the_view(world: World) -> None:
    """A change that cannot be appended leaves the view as it was."""
    with closing(sqlite3.connect(world.db)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER no_changes BEFORE INSERT ON place_changes "
            "BEGIN SELECT RAISE(ABORT, 'log is full'); END",
        )
    with pytest.raises(sqlite3.Error, match="log is full"):
        places.save(world.db, name="gym", lat=48.005, lng=-123.0, origin="user")
    assert world.rows() == []
    assert world.rows("place_changes") == []


def test_undo_appends_the_inverse_and_a_later_change_to_the_place_refuses_it(
    world: World,
) -> None:
    """save, rename, forget; undo the forget; the rename is now behind the undo; undo the undo."""
    saved = places.save(world.db, name="gym", lat=48.005, lng=-123.0, origin="model")
    place_id = saved.place.id
    renamed = places.edit(world.db, place_id, name="Iron Works", origin="page")
    forgotten = places.forget(world.db, place_id, origin="page")
    with pytest.raises(Conflict, match="a later change touched the same place"):
        places.undo(world.db, str(saved.change_id), origin="page")
    with pytest.raises(Conflict):
        places.undo(world.db, str(renamed.change_id), origin="page")
    assert world.kept() == []
    back = places.undo(world.db, str(forgotten.change_id), origin="page")
    (restored,) = world.kept()
    assert (restored.id, restored.name, restored.lat) == (place_id, "Iron Works", 48.005)
    assert back.place.name == "Iron Works"
    # The rename is behind the undo now, and so is the forget it undid.
    for change in (renamed, forgotten):
        with pytest.raises(Conflict):
            places.undo(world.db, str(change.change_id), origin="page")
    places.undo(world.db, str(back.change_id), origin="page")  # an undo can be taken back
    assert world.kept() == []
    assert [r["op"] for r in world.rows("place_changes")] == [
        "save", "rename", "forget", "undo", "undo",
    ]
    assert [r["origin"] for r in world.rows("place_changes")] == [
        "model", "page", "page", "page", "page",
    ]
    with pytest.raises(LookupError):
        places.undo(world.db, "place-change:none", origin="page")
    with pytest.raises(ValueError, match="origin"):
        places.undo(world.db, str(back.change_id), origin="nobody")


def test_undoing_a_forget_when_its_name_has_been_kept_since_is_refused(world: World) -> None:
    """The name is unique: bringing the old place back would make two of it."""
    old = places.save(world.db, name="Gym", lat=48.005, lng=-123.0, origin="user")
    gone = places.forget(world.db, old.place.id, origin="user")
    places.save(world.db, name="gym", lat=48.020, lng=-123.0, origin="user")
    with pytest.raises(Conflict, match="kept now under that name"):
        places.undo(world.db, str(gone.change_id), origin="page")


def test_a_second_visit_to_the_same_circle_under_a_new_name_is_a_rename_not_a_save(
    world: World,
) -> None:
    """`save` refuses it and says which place holds the spot."""
    places.save(world.db, name="gym", lat=48.005, lng=-123.0, origin="user")
    with pytest.raises(ValueError, match="already kept as “gym”"):
        places.save(world.db, name="Dojo", lat=48.0051, lng=-123.0, origin="user")
    again = places.save(world.db, name="GYM", lat=48.0051, lng=-123.0, origin="user")
    assert again.change_id is None


# --- the memory page -------------------------------------------------------------------------


@pytest.fixture
def page(world: World) -> TestClient:
    """The real routes over the real page, on the world's store and log."""
    memory_page = MemoryPage(world.db, None, history_since="", event_log=world.log, zone=ZONE)
    memory_page.clock = lambda: NOW_MS / 1000  # the 28 days end on the world's day, not today
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            memory_page=memory_page,
        ),
    )
    return TestClient(app)


def test_the_page_lists_edits_forgets_and_takes_back(world: World, page: TestClient) -> None:
    """GET shows the kept places with facts and the changes; POST renames, forgets, undoes."""
    gym = places.save(world.db, name="gym", lat=48.005, lng=-123.0, role="other", origin="model")
    home = places.save(world.db, name="home", lat=48.0, lng=-123.0, role="home", origin="user")
    listing = page.get("/inherent/memory/places").json()
    by_name = {p["name"]: p for p in listing["places"]}
    assert by_name["gym"]["days_28"] == 11
    assert by_name["gym"]["last_visit"] == "2026-10-09"
    assert (by_name["home"]["days_28"], by_name["home"]["median_stay_min"]) == (27, 240)
    assert by_name["home"]["phone_label"] == "7 Dwelling Rd"
    assert [c["op"] for c in listing["changes"]] == ["save", "save"]
    assert [c["undoable"] for c in listing["changes"]] == [True, True]
    assert listing["changes"][0]["place_id"] == home.place.id
    renamed = page.post(
        f"/inherent/memory/places/{gym.place.id}", json={"name": "Iron Works", "role": "other"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["place"]["name"] == "Iron Works"
    # The save of the gym is behind the rename now: its undo is refused, and says so.
    changes = page.get("/inherent/memory/places").json()["changes"]
    assert [(c["op"], c["undoable"]) for c in changes] == [
        ("rename", True), ("save", True), ("save", False),
    ]
    stale = page.post("/inherent/memory/places/undo", json={"change_id": changes[2]["id"]})
    assert stale.status_code == 409
    assert "later change" in stale.json()["detail"]
    gone = page.post(f"/inherent/memory/places/{gym.place.id}", json={"forget": True})
    assert gone.status_code == 200
    assert [p["name"] for p in page.get("/inherent/memory/places").json()["places"]] == ["home"]
    last = page.get("/inherent/memory/places").json()["changes"][0]
    assert (last["op"], last["origin"]) == ("forget", "page")
    back = page.post("/inherent/memory/places/undo", json={"change_id": last["id"]})
    assert back.status_code == 200
    assert {p["name"] for p in page.get("/inherent/memory/places").json()["places"]} == {
        "home", "Iron Works",
    }
    # The refusals: no such place, a role that is not one, a forget with a name, no change.
    assert page.post("/inherent/memory/places/place:nope", json={"name": "x"}).status_code == 404
    assert page.post(
        f"/inherent/memory/places/{home.place.id}", json={"role": "castle"},
    ).status_code == 400
    assert page.post(
        f"/inherent/memory/places/{home.place.id}", json={"forget": True, "name": "x"},
    ).status_code == 400
    assert page.post(f"/inherent/memory/places/{home.place.id}", json={}).status_code == 400
    assert page.post(
        "/inherent/memory/places/undo", json={"change_id": "place-change:none"},
    ).status_code == 404


# --- asking once -----------------------------------------------------------------------------


def lines(
    world: World, age_min: int, centre: tuple[float, float], *, left: bool = False,
) -> list[str]:
    """The `[Place]` lines of a turn half an hour after the last, the fix ``age_min`` old.

    The latest fix is a visit at ``centre`` that began ``age_min`` minutes ago (or ended then,
    when ``left``).
    """
    world.fixes += 1
    now_ms = NOW_MS + world.fixes * 30 * MINUTE
    at_ms = now_ms - age_min * MINUTE
    if left:
        stay(world.conn, f"fix-{world.fixes}", *centre, at_ms - 10 * MINUTE, at_ms)
    else:
        stay(world.conn, f"fix-{world.fixes}", *centre, at_ms)
    where = Whereabouts(world.tmp, None, None, memory_db=world.db, zone=ZONE)
    where.clock = lambda: now_ms / 1000
    return [line for line in where.lines(world.conn) if line.startswith("[Place]")]


def test_the_line_for_an_unnamed_spot_comes_once(world: World) -> None:
    """The approved text, with the days before today and the last of them; never again for it."""
    cafe = CAFE.centre()
    first = lines(world, 3, cafe)
    assert first == [
        "[Place] He is at a spot he has been to on 6 days in the last 4 weeks (last 2026-10-08). "
        "It has no name. If the moment suits, you may ask him once, in passing, what it is. "
        "This note is not shown again.",
    ]
    assert first[0] == ASK_PLACE_LINE.format(n=6, date="2026-10-08")
    assert lines(world, 2, cafe) == []
    assert lines(world, 1, (cafe[0] + 0.0005, cafe[1])) == []  # 55 m on is the same spot
    (prompt,) = world.rows("place_prompts")
    assert (round(prompt["lat"], 6), round(prompt["lng"], 6)) == tuple(round(v, 6) for v in cafe)
    # Another spot 145 m from the one he was asked about is its own.
    assert len(lines(world, 2, NORTH.centre())) == 1
    assert len(world.rows("place_prompts")) == 2


def test_it_needs_three_days_a_fresh_fix_and_an_unkept_unasked_spot(world: World) -> None:
    """Too few days, a stale fix, a kept place and a spot already asked about all stay silent."""
    assert lines(world, 3, SHOP.centre()) == []  # one day in four weeks
    assert lines(world, 20, NORTH2.centre()) == []  # stale, and it does not use the spot up
    assert world.rows("place_prompts") == []
    assert lines(world, 3, NORTH2.centre(), left=True) == []  # he is leaving it
    places.save(world.db, name="gym", lat=GYM.centre()[0], lng=GYM.centre()[1], origin="user")
    assert lines(world, 3, GYM.centre()) == []  # kept: it has a name
    with closing(sqlite3.connect(world.db)) as conn, conn:  # asked about 44 m from it
        conn.execute(
            "INSERT INTO place_prompts (id, lat, lng, shown_at) VALUES ('seen', ?, ?, 't')",
            (NORTH2.centre()[0] + 0.0004, NORTH2.centre()[1]),
        )
    assert lines(world, 3, NORTH2.centre()) == []
    # Fresh at 15 minutes, exactly three days, unkept and new: the edge of all three holds.
    assert len(lines(world, 15, NORTH.centre())) == 1


def test_whereabouts_without_a_store_never_asks(world: World) -> None:
    """The line needs the store and the zone; the other lines are as they were."""
    stay(world.conn, "fix", *CAFE.centre(), NOW_MS - MINUTE, label="Lotus Rd")
    where = Whereabouts(world.tmp, None, None)
    where.clock = lambda: NOW_MS / 1000
    found = where.lines(world.conn)
    assert not [line for line in found if line.startswith("[Place]")]
    assert any(line.startswith("His phone last put him at Lotus Rd") for line in found)


# --- the prompt ------------------------------------------------------------------------------


def context(world: World, now: datetime = NOW, **kept: Any) -> str:  # noqa: ANN401
    """The system prompt's profile for a turn at ``now``."""
    return render_context(
        world.db, exclude_id="", now=now,
        places=partial(_places_block, world.db, world.log, ZONE), **kept,
    ).profile


def test_the_block_follows_about_the_user_and_leaves_what_is_before_it_alone(
    world: World,
) -> None:
    """One line per place, rate over complete days; empty block omitted; prefix byte-stable."""
    remember_fact(world.db, "城市", "Victoria")
    base = render_context(world.db, exclude_id="", now=NOW).profile
    assert base.startswith("[About the user]")
    assert context(world) == base  # nothing kept: no block
    places.save(world.db, name="Iron Works", lat=48.005, lng=-123.0, origin="model")
    places.save(world.db, name="home", lat=48.0, lng=-123.0, role="home", origin="user")
    places.save(world.db, name="Library", lat=48.0800, lng=-123.0, origin="user")
    places.migrate_settings(world.db, {"school": "1 Example Campus Way"})
    block = context(world)
    assert block == (
        base + "\n\n[Places he keeps]\n"
        "Iron Works · other · ~3 days a week over the last 4 weeks · last visit 2026-10-09\n"
        "home · home · ~7 days a week over the last 4 weeks · last visit 2026-10-10\n"
        "Library · other · not visited in the last 4 weeks\n"
        "school · school · not visited in the last 4 weeks"
    )
    # A visit today, or a place he goes to, moves nothing before the block and not the block.
    stay(
        world.conn, "today", 48.0050, -123.0,
        ms_days_ago(0, 9), ms_days_ago(0, 10), "Iron Works Gym",
    )
    assert context(world) == block
    # The next day counts it, and the text before the block is still byte for byte the same.
    tomorrow = context(world, NOW + timedelta(days=1))
    assert tomorrow.startswith(base + "\n\n[Places he keeps]\n")
    assert "Iron Works · other · ~3 days a week" in tomorrow
    assert "last visit 2026-10-11" in tomorrow
    assert tomorrow != block
    # A new place appends a line; everything before the block is untouched.
    places.save(world.db, name="Dojo", lat=48.0900, lng=-123.0, origin="user")
    assert context(world).startswith(base + "\n\n[Places he keeps]\n")
    assert context(world).endswith("\nDojo · other · not visited in the last 4 weeks")


def test_the_block_stands_alone_without_a_note_and_ahead_of_the_ledger(world: World) -> None:
    """No core memory: just the block. With a ledger: about, places, then its standing text."""
    places.save(world.db, name="home", lat=48.0, lng=-123.0, role="home", origin="user")
    alone = context(world)
    assert alone.startswith("[Places he keeps]\nhome · home · ")
    remember_fact(world.db, "城市", "Victoria")
    ledger = lambda _now, _last: ("STANDING", "PER TURN")  # noqa: E731
    joined = context(world, ledger=ledger)
    about, block, standing = joined.split("\n\n")
    assert about.startswith("[About the user]")
    assert block.startswith("[Places he keeps]\n")
    assert standing == "STANDING"
    assert render_context(world.db, exclude_id="", now=NOW, ledger=ledger).profile == (
        about + "\n\nSTANDING"
    )


def test_a_failing_block_leaves_the_prompt_whole(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The block never fails a turn: a read that raises is the block left out."""
    places.save(world.db, name="home", lat=48.0, lng=-123.0, role="home", origin="user")

    def broken(*_args: object, **_kwargs: object) -> str:
        msg = "the log fell over"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(places, "prompt_block", broken)
    assert _places_block(world.db, world.log, ZONE, NOW) == ""
    assert render_context(world.db, exclude_id="", now=NOW).profile == ""


# --- what else reads the places --------------------------------------------------------------


def day_stays(world: World) -> list[dict[str, Any]]:
    """The stays of today as ``GET /inherent/day`` serves them."""
    served = read_day(world.log, ZONE, world.db, None, TODAY, NOW_MS)
    return [item for item in served["items"] if item["kind"] == "stay"]


def test_the_day_line_gives_each_stay_the_smallest_kept_circle_that_holds_it(
    world: World,
) -> None:
    """`kept_place` {id, name, role}; the phone's string stays; no place, no key."""
    campus = places.save(
        world.db, name="Campus", lat=48.0500, lng=-123.0000, radius_m=600, origin="user",
    ).place
    library = places.save(
        world.db, name="Library", lat=48.0510, lng=-123.0000, radius_m=80, origin="user",
    ).place
    stay(world.conn, "t1", 48.0509, -123.0000, ms_days_ago(0, 8), ms_days_ago(0, 9), "Main Library")
    stay(world.conn, "t2", 48.0500, -123.0001, ms_days_ago(0, 9, 30), ms_days_ago(0, 10), "Quad")
    stay(
        world.conn, "t3", 48.0700, -123.0000,
        ms_days_ago(0, 10, 30), ms_days_ago(0, 11), "Elsewhere",
    )
    found = {item["id"]: item for item in day_stays(world)}
    assert found["t1"]["kept_place"] == {"id": library.id, "name": "Library", "role": "other"}
    assert found["t2"]["kept_place"] == {"id": campus.id, "name": "Campus", "role": "other"}
    assert "kept_place" not in found["t3"]
    assert [found[k]["place"] for k in ("t1", "t2", "t3")] == ["Main Library", "Quad", "Elsewhere"]
    # Without the memory store the day is what it was.
    plain = read_day(world.log, ZONE, None, None, TODAY, NOW_MS)
    assert not [i for i in plain["items"] if "kept_place" in i]
    # Forget the library: the smaller circle is gone, the campus holds the stay.
    places.forget(world.db, library.id, origin="user")
    again = {item["id"]: item for item in day_stays(world)}
    assert again["t1"]["kept_place"]["name"] == "Campus"


def test_the_ledger_names_a_stay_by_its_kept_place_and_rebuilds_when_places_change(
    tmp_path: Path,
) -> None:
    """A kept place names the stay (even an unlabelled one); the cached text follows."""
    src = ledger_world(tmp_path)
    monday = datetime(2026, 10, 5, tzinfo=LEDGER_ZONE)

    def ms(hour: int) -> int:
        return int((monday + timedelta(hours=hour)).timestamp() * 1000)

    with closing(open_event_log(src.event_log)) as conn:
        stay(conn, "m1", 48.0000, -123.0000, ms(10), ms(12), "7 Dwelling Rd")
        stay(conn, "m2", 48.0002, -123.0001, ms(13), ms(14))  # the phone had no name for it
        stay(conn, "m3", 48.0300, -123.0000, ms(15), ms(16), "Corner Shop")
    ledger = LedgerContext(src, LedgerSettings())
    before = ledger(LEDGER_NOW, None)[0]
    assert "Places (phone): 7 Dwelling Rd 2h00m, Corner Shop 1h00m" in before
    places.save(src.memory_db, name="家", lat=48.0, lng=-123.0, role="home", origin="user")
    after = ledger(LEDGER_NOW, None)[0]  # the places stamp is in the cache key
    assert after != before
    assert "Places (phone): 家 3h00m, Corner Shop 1h00m" in after
    assert "7 Dwelling Rd" not in after
    assert ledger(LEDGER_NOW, None)[0] is after  # nothing changed: the same cached text


# --- home and school -------------------------------------------------------------------------


def _body(seen: list[Any]) -> dict[str, Any]:
    """The JSON the one routes request carried."""
    data = sent_request(seen)[0].data
    assert isinstance(data, bytes)
    return dict(json.loads(data))


def test_the_settings_are_copied_once_and_transit_and_the_rides_read_the_table(
    world: World, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lat,lng is a centre, an address is an address; a forgotten copy is not copied again."""
    config = {
        "home": {"weather": {"latitude": 48.0, "longitude": -123.0}},
        "transit": {"school": "Example Campus, Example City"},
    }
    copied = places.migrate_settings(world.db, _transit_places(config))
    assert [(p.name, p.role, p.lat, p.lng, p.address, p.origin) for p in copied] == [
        ("home", "home", 48.0, -123.0, None, "settings"),
        ("school", "school", None, None, "Example Campus, Example City", "settings"),
    ]
    changes = [(r["op"], r["origin"]) for r in world.rows("place_changes")]
    assert changes == [("save", "settings")] * 2
    assert places.home_school(world.db) == {
        "home": "48.0,-123.0", "school": "Example Campus, Example City",
    }
    # Once: a second boot with other keys copies nothing, even after he forgot one.
    home = world.kept()[0]
    places.forget(world.db, home.id, origin="user")
    assert places.migrate_settings(world.db, {"home": "48.9,-123.9", "school": "x"}) == []
    assert [p.role for p in world.kept()] == ["school"]
    places.save(world.db, name="home", lat=48.0, lng=-123.0, role="home", origin="user")
    # `bus` reads home and school from the table at each call.
    seen = serve_routes(monkeypatch)
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)
    registry = build_default_registry(
        transit_api_key=FAKE_KEY, transit_places=partial(places.home_school, world.db),
    )
    (transit,) = (d for d in registry.get_definitions() if d.name == "bus")
    ctx = SimpleNamespace(conn=world.conn, action_id="A1")
    transit.handler({"from": "home", "to": "school"}, ctx)  # type: ignore[arg-type]
    sent = _body(seen)
    assert sent["origin"] == {"location": {"latLng": {"latitude": 48.0, "longitude": -123.0}}}
    assert sent["destination"] == {"address": "Example Campus, Example City"}
    school = next(p for p in world.kept() if p.role == "school")
    places.forget(world.db, school.id, origin="user")
    places.save(world.db, name="school", lat=48.5, lng=-123.5, role="school", origin="user")
    seen.clear()
    transit.handler({"from": "home", "to": "school"}, ctx)  # type: ignore[arg-type]
    sent = _body(seen)
    assert sent["destination"] == {"location": {"latLng": {"latitude": 48.5, "longitude": -123.5}}}
    # The rides find his stop from the same table.
    rides = Rides(world.log, None, places=partial(places.home_school, world.db))
    assert rides._place(world.conn, "School") == (48.5, -123.5)  # noqa: SLF001
    home_now = next(p for p in world.kept() if p.role == "home")
    places.forget(world.db, home_now.id, origin="user")
    assert rides._place(world.conn, "home") is None  # noqa: SLF001


def test_the_tables_and_the_nightly_prompt_are_what_the_decision_names(tmp_path: Path) -> None:
    """The columns of the three tables, and the sentence added to the consolidation prompt."""
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        columns = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("places", "place_changes", "place_prompts")
        }
    assert columns == {
        "places": [
            "id", "name", "role", "lat", "lng", "radius_m", "address", "origin", "created_at",
            "updated_at",
        ],
        "place_changes": ["id", "ts", "origin", "op", "place_id", "before", "after"],
        "place_prompts": ["id", "lat", "lng", "shown_at"],
    }
    config = yaml.safe_load((Path(__file__).parents[2] / "config" / "jarvis.yaml").read_text())
    prompt: str = config["core_memory"]["prompt"]
    assert prompt.rstrip().endswith(
        "- Places he keeps are listed in [Places he keeps]. Do not add or keep items\n"
        "  that only restate a place's name or location.",
    )


def test_the_nightly_consolidator_reads_the_places_he_keeps(tmp_path: Path) -> None:
    """The sentence added to its prompt points at a block its input carries (ADR 0234)."""
    path = tmp_path / "memory.db"
    seed_days(path, 2)
    model = consolidator()
    core_schedule(tmp_path, model).write(CORE_TODAY)
    asked = consolidation_input(model, day_ago(2).isoformat())
    assert "[Places he keeps]" not in asked  # none kept: no block

    places.save(path, name="gym", lat=48.0, lng=-123.0, role="other", origin="model")
    places.save(path, name="home", lat=48.01, lng=-123.0, role="home", origin="settings")
    seed_days(path, 1)
    core_schedule(tmp_path, model).write(CORE_TODAY)
    asked = consolidation_input(model, day_ago(1).isoformat())
    assert "[Current core memory]\n" in asked
    assert "\n\n[Places he keeps]\ngym · other\nhome · home\n\n[Summary of " in asked
