"""ADR 0284: what is around him, for `where_am_i` and for one line of the state block.

Every Google request is a stub: `urllib.request.urlopen` is replaced, so a request to a host the
stub does not know fails the test instead of going out. Built over a real event log and a real
memory.db.

- `where_am_i` carries the street and nearest intersection, the candidates and the other places
  within 300 m with direction, distance and open now, the kept place he is in, the past stays and
  how long he has been here; the key is in no result;
- a candidate comes from a Nearby Search combined with the stay and a kept place;
- a point within 50 m of a held one pays nothing, a farther one or a later one pays again;
- near the cap neither request is made and the fix still comes with a plain reason;
- a project without Places (or Geocoding) enabled still gets the other part and the reason;
- the state block's line shows what is held for his position, changes with a move, and never
  makes a request.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution import around, maps_spend, places_api
from jarvis.execution.location_tool import build_where_tool
from jarvis.runtime.whereabouts import Whereabouts
from jarvis.state import places
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

KEY = "test-key-not-real"
ZONE = ZoneInfo("America/Vancouver")
HERE = (48.4284, -123.3656)
MIN_MS = 60_000
GEOCODE = "https://maps.googleapis.com/maps/api/geocode/json"
NEARBY = "https://places.googleapis.com/v1/places:searchNearby"


def _shop(n: int, north_m: float, *, open_now: bool | None = True) -> dict[str, Any]:
    """A Places API place ``north_m`` metres north of HERE."""
    raw: dict[str, Any] = {
        "id": f"ChIJshop{n}",
        "displayName": {"text": f"Shop {n}", "languageCode": "en"},
        "formattedAddress": f"{n} Fort St, Victoria, BC V8W 1E5, Canada",
        "location": {"latitude": HERE[0] + north_m / 111_320, "longitude": HERE[1]},
        "primaryTypeDisplayName": {"text": "Ramen restaurant"},
    }
    if open_now is not None:
        raw["currentOpeningHours"] = {"openNow": open_now}
    return raw


STREET = {
    "status": "OK",
    "results": [
        {"types": ["intersection"], "formatted_address": "Fort St & Douglas St, Victoria, BC"},
        {
            "types": ["street_address"],
            "formatted_address": "1234 Fort St, Victoria, BC V8W 1E5, Canada",
            "address_components": [
                {"long_name": "1234", "types": ["street_number"]},
                {"long_name": "Fort Street", "types": ["route"]},
            ],
        },
    ],
}


class Google:
    """Stands in for the Geocoding and Places hosts; what was asked is in ``seen``."""

    def __init__(self) -> None:
        """Start with an answer for each host."""
        self.seen: list[urllib.request.Request] = []
        # 12 places; Shop 1 is 8 m away, Shop 2 is 40 m, the rest are 60 m and farther.
        self.nearby: Any = {
            "places": [_shop(1, 8), _shop(2, 40), *(_shop(n, 40 + 20 * n) for n in range(3, 13)),
                       _shop(13, 500)],
        }
        self.street: Any = STREET

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> io.BytesIO:  # noqa: ARG002
        """Answer one request."""
        self.seen.append(request)
        url = request.full_url
        if url.startswith(GEOCODE):
            reply = self.street
        elif url == NEARBY:
            reply = self.nearby
        else:
            msg = f"a request to a host the test does not stub: {url}"
            raise AssertionError(msg)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps(reply).encode())

    def asked(self, host: str) -> int:
        """How many requests went to ``host``."""
        return sum(1 for r in self.seen if r.full_url.startswith(host))


def _disabled() -> urllib.error.HTTPError:
    body = json.dumps({"error": {"status": "PERMISSION_DENIED",
                                 "details": [{"reason": "SERVICE_DISABLED"}]}}).encode()
    return urllib.error.HTTPError(NEARBY, 403, "denied", {}, io.BytesIO(body))  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def google(monkeypatch: pytest.MonkeyPatch) -> Google:
    """Google."""
    stub = Google()
    monkeypatch.setattr(urllib.request, "urlopen", stub)
    return stub


class World:
    """A log, a memory.db and a clock he can move on."""

    def __init__(self, tmp_path: Path, cap: float = 10.0, key: str | None = KEY) -> None:
        """An empty world at the current time."""
        self.log: sqlite3.Connection = open_event_log(tmp_path / "events.db")
        self.db = tmp_path / "memory.db"
        self.now = time.time()
        self.root = tmp_path
        self.surroundings = around.Surroundings(key, cap, self.db, ZONE, lambda: self.now)
        self.tool = build_where_tool(None, self._phone, self.surroundings)[0]

    def _phone(self) -> dict[str, Any]:
        return {
            "lat": self.at[0], "lng": self.at[1], "accuracy_m": 15.0, "age_s": 5,
            "device": "iphone", "source": "phone", "fresh": True,
        }

    at: tuple[float, float] = HERE

    def fix(self, ago_min: float, at: tuple[float, float] | None = None) -> None:
        """A location report ``ago_min`` minutes ago."""
        lat, lng = at or self.at
        emit_event(
            self.log, type="phone.location_observed", ingestion_node="iphone",
            payload={"lat": lat, "lng": lng, "accuracy_m": 15.0},
            ts_epoch_ms=int((self.now - ago_min * 60) * 1000),
        )

    def visit(self, days_ago: int, at: tuple[float, float]) -> None:
        """A completed hour-long stay ``days_ago`` days back."""
        end = int(self.now * 1000) - days_ago * 86_400_000
        emit_event(
            self.log, type="phone.visit_observed", ingestion_node="iphone",
            payload={"lat": at[0], "lng": at[1], "accuracy_m": 30.0,
                     "arrived_at_ms": end - 60 * MIN_MS, "departed_at_ms": end},
            ts_epoch_ms=end,
        )

    def ask(self) -> dict[str, Any]:
        """`where_am_i` as the model calls it."""
        ctx = SimpleNamespace(conn=self.log, action_id="A1")
        return dict(self.tool.handler({}, ctx))  # type: ignore[arg-type]

    def costs(self) -> list[tuple[str, float]]:
        """The (SKU, USD) rows recorded, oldest first."""
        rows = self.log.execute(
            "SELECT json_extract(payload_json, '$.sku'), json_extract(payload_json, '$.cost_usd') "
            "FROM events WHERE type = 'maps.spend_recorded' ORDER BY id",
        ).fetchall()
        return [(sku, usd) for sku, usd in rows]

    def lines(self) -> list[str]:
        """The state block's lines about his surroundings."""
        where = Whereabouts(
            self.root, None, None, memory_db=self.db, zone=ZONE, around=self.surroundings,
        )
        where.clock = lambda: self.now
        return [ln for ln in where.lines(self.log) if ln.startswith("Around him")]


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A world with his phone at HERE."""
    return World(tmp_path)


def _east(m: float) -> tuple[float, float]:
    """HERE moved ``m`` metres east."""
    return (HERE[0], HERE[1] + m / 74_000)


# --- where_am_i -----------------------------------------------------------------------------


def test_where_am_i_adds_the_street_the_candidates_and_the_places_around(world: World) -> None:
    """Street, intersection, address; near places split into candidates and ten others."""
    out = world.ask()

    assert out["lat"] == HERE[0]  # the fix is still there
    here = out["around"]
    assert (here["street"], here["intersection"], here["address"]) == (
        "Fort Street", "Fort St & Douglas St", "1234 Fort St",
    )
    assert [c["name"] for c in here["candidates"]] == ["Shop 1", "Shop 2"]
    assert here["candidates"][0] == {
        "name": "Shop 1", "kind": "Ramen restaurant", "distance_m": 8, "direction": "north",
        "open_now": True, "address": "1 Fort St, Victoria, BC V8W 1E5, Canada",
        "place_id": "ChIJshop1", "past_stays": 0,
    }
    assert len(here["nearby"]) == 10  # ten of the eleven others within 300 m; Shop 13 is 500 m
    assert all(p["distance_m"] <= 300 and "direction" in p for p in here["nearby"])
    metres = [p["distance_m"] for p in here["nearby"]]
    assert metres == sorted(metres)
    assert "Shop 13" not in {p["name"] for p in here["nearby"]}
    assert here["looked_up"] == "just now"
    assert "save_place" in here["note"]
    assert KEY not in json.dumps(out)


def test_the_requests_ask_for_the_cheapest_fields_and_are_priced(
    world: World, google: Google,
) -> None:
    """One filtered reverse geocode and one Nearby Search; 0.005 + 0.035 recorded."""
    world.ask()

    [geo] = [r for r in google.seen if r.full_url.startswith(GEOCODE)]
    assert "result_type=street_address%7Cintersection" in geo.full_url
    [near] = [r for r in google.seen if r.full_url == NEARBY]
    assert near.get_header("X-goog-fieldmask") == places_api.AROUND_MASK
    assert near.get_header("X-goog-api-key") == KEY
    body = json.loads(near.data)  # type: ignore[arg-type]
    assert body["locationRestriction"]["circle"]["radius"] == 300
    assert "includedTypes" not in body
    assert world.costs() == [
        ("geocoding.reverse", 0.005), ("places.nearby_search.enterprise", 0.035),
    ]


def test_a_candidate_comes_from_the_stay_the_kept_place_and_his_past_visits(
    world: World,
) -> None:
    """Forty minutes here, a kept place round the point, earlier stays at Shop 1's door."""
    world.fix(80, at=_east(3000))  # a far fix before the run: it ends the run, not part of it
    for ago in (40, 30, 20, 10, 1):
        world.fix(ago)
    shop1 = (HERE[0] + 8 / 111_320, HERE[1])
    world.visit(3, shop1)
    world.visit(9, shop1)
    places.save(world.db, name="Ramen corner", lat=HERE[0], lng=HERE[1], origin="user")

    here = world.ask()["around"]

    assert here["here_for_min"] == 40
    assert here["kept_place"]["name"] == "Ramen corner"
    assert here["kept_place"]["days_28"] == 2  # both stays are inside its circle
    shop = here["candidates"][0]
    assert (shop["name"], shop["past_stays"]) == ("Shop 1", 2)
    assert "unkept_spot" not in here


def test_a_spot_he_keeps_going_to_that_is_not_kept_is_reported_with_its_counts(
    world: World,
) -> None:
    """No kept place: the unkept spot's counts come instead."""
    world.visit(2, HERE)
    world.visit(4, HERE)

    here = world.ask()["around"]

    assert "kept_place" not in here
    assert here["unkept_spot"]["days_84"] == 2


# --- the cache ------------------------------------------------------------------------------


def test_a_point_within_fifty_metres_pays_nothing_and_a_far_or_late_one_pays_again(
    world: World, google: Google,
) -> None:
    """Same spot, 30 m away, 120 m away, and three hours on."""
    world.ask()
    assert len(world.costs()) == 2

    world.at = _east(30)
    again = world.ask()["around"]
    assert len(google.seen) == 2
    assert len(world.costs()) == 2
    assert again["candidates"][0]["distance_m"] != 8  # measured from where he is now
    assert again["intersection"] == "Fort St & Douglas St"

    world.at = _east(120)
    world.ask()
    assert len(google.seen) == 4
    assert len(world.costs()) == 4

    world.now += around.TTL_S + 1
    world.at = HERE
    world.ask()
    assert len(google.seen) == 6


# --- the cap and the missing APIs ------------------------------------------------------------


def test_near_the_cap_no_request_is_made_and_the_fix_still_comes_with_the_reason(
    tmp_path: Path, google: Google,
) -> None:
    """9.5 of 10 spent: both parts refused with the cap's plain reason; kept place still there."""
    world = World(tmp_path, cap=10.0)
    places.save(world.db, name="Home", lat=HERE[0], lng=HERE[1], origin="user")
    maps_spend.PRICE_USD["test.spend"] = 9.5
    try:
        maps_spend.record(SimpleNamespace(conn=world.log, action_id="A-seed"), ["test.spend"])  # type: ignore[arg-type]
        out = world.ask()
    finally:
        maps_spend.PRICE_USD.pop("test.spend", None)

    assert google.seen == []
    assert out["lat"] == HERE[0]
    assert out["around"]["kept_place"]["name"] == "Home"
    assert "candidates" not in out["around"]
    assert len(out["around"]["unavailable"]) == 2
    assert all("Map lookups are paused" in why for why in out["around"]["unavailable"])


def test_without_places_enabled_the_street_and_kept_place_still_come_with_the_reason(
    world: World, google: Google,
) -> None:
    """A 403 SERVICE_DISABLED on Nearby Search costs nothing and is named."""
    google.nearby = _disabled()
    places.save(world.db, name="Home", lat=HERE[0], lng=HERE[1], origin="user")

    out = world.ask()
    here = out["around"]

    assert here["intersection"] == "Fort St & Douglas St"
    assert here["kept_place"]["name"] == "Home"
    assert "candidates" not in here
    [why] = here["unavailable"]
    assert "Places API (New) is not enabled" in why
    assert world.costs() == [("geocoding.reverse", 0.005)]  # the refused request is not billed

    google.nearby = {"places": [_shop(1, 8)]}  # switched on: the next call fills the gap, only it
    again = world.ask()["around"]
    assert "unavailable" not in again
    assert [c["name"] for c in again["candidates"]] == ["Shop 1"]
    assert google.asked(GEOCODE) == 1  # the street was held


def test_a_geocoding_api_that_is_off_leaves_the_places_and_says_why(
    world: World, google: Google,
) -> None:
    """REQUEST_DENIED from the Geocoding API: no street, the places come, no charge for it."""
    google.street = {"status": "REQUEST_DENIED", "error_message": "This API is not activated."}

    here = world.ask()["around"]

    assert "intersection" not in here
    assert [c["name"] for c in here["candidates"]] == ["Shop 1", "Shop 2"]
    [why] = here["unavailable"]
    assert "Geocoding" in why
    assert world.costs() == [("places.nearby_search.enterprise", 0.035)]


def test_without_a_google_key_the_free_parts_come_with_the_reason(tmp_path: Path) -> None:
    """No key: kept place and stay length, and a reason for each part that needs Google."""
    world = World(tmp_path, key=None)
    places.save(world.db, name="Home", lat=HERE[0], lng=HERE[1], origin="user")
    world.fix(25)

    here = world.ask()["around"]

    assert (here["kept_place"]["name"], here["here_for_min"]) == ("Home", 25)
    assert len(here["unavailable"]) == 2


# --- the state block's line -------------------------------------------------------------------


def test_the_line_never_asks_google_and_shows_only_what_is_free_until_something_is_held(
    world: World, google: Google,
) -> None:
    """Nothing held, no kept place: no line; with a kept place its name; no request."""
    world.fix(1)
    assert world.lines() == []

    places.save(world.db, name="Home", lat=HERE[0], lng=HERE[1], origin="user")
    assert world.lines() == ["Around him: at Home."]
    assert google.seen == []
    assert world.costs() == []


def test_the_line_names_the_street_and_the_probable_place_and_moves_with_him(
    world: World, google: Google,
) -> None:
    """After a lookup: street, and the nearest place once he has stayed ten minutes.

    A small step changes nothing, a new spot with nothing held shows nothing, and no line costs
    a request.
    """
    for ago in (30, 20, 10, 1):
        world.fix(ago)
    world.ask()
    spent = len(google.seen)

    assert world.lines() == ["Around him: Fort St & Douglas St; maybe at Shop 1."]

    world.fix(0, at=_east(25))  # a step of 25 m: the same cell
    assert world.lines() == ["Around him: Fort St & Douglas St; maybe at Shop 1."]

    world.fix(0, at=_east(400))  # a new spot, nothing held for it
    assert world.lines() == []
    assert len(google.seen) == spent


def test_a_short_stop_gives_the_street_alone(world: World) -> None:
    """Passing by: no fix is older than a few minutes, so no place is guessed."""
    for ago in (4, 1):
        world.fix(ago)
    world.ask()

    assert world.lines() == ["Around him: Fort St & Douglas St."]


def test_a_kept_place_beats_a_guess(world: World) -> None:
    """In a kept place the line says it, not the nearest shop."""
    for ago in (30, 20, 10, 1):
        world.fix(ago)
    places.save(world.db, name="Home", lat=HERE[0], lng=HERE[1], origin="user")
    world.ask()

    assert world.lines() == ["Around him: Fort St & Douglas St; at Home."]


def test_the_line_goes_when_the_report_or_the_held_reading_is_old(world: World) -> None:
    """Three hours on, the fix and the held reading are both too old to say what is around him."""
    world.fix(1)
    world.ask()
    assert world.lines()

    world.now += around.TTL_S + 60
    assert world.lines() == []


def test_the_runtime_gives_the_state_block_the_line_and_makes_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, google: Google,
) -> None:
    """Through the function every turn calls: a kept place under the phone's fix is a line."""
    from jarvis.runtime import _whereabouts_lines, bootstrap_runtime_app  # noqa: PLC0415

    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text("assistant_name: Jarvis\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    runtime = bootstrap_runtime_app(runtime_root=root)
    try:
        assert runtime.memory is not None
        places.save(runtime.memory.db_path, name="Home", lat=HERE[0], lng=HERE[1], origin="user")
        emit_event(
            runtime.conn, type="phone.location_observed", ingestion_node="iphone",
            payload={"lat": HERE[0], "lng": HERE[1], "accuracy_m": 15.0},
        )
        assert "Around him: at Home." in _whereabouts_lines(runtime)
        assert google.seen == []
    finally:
        runtime.conn.close()
