"""ADR 0355: a plan reads the weather of an outing's own hours at its own place, over the real home.

The real ``Home`` over the fake WeatherKit of ``weatherkit_world`` (a throwaway key, no network),
the real ``PlanWatch`` over fresh stores, and a stub model and legs. The day is Wednesday
2026-10-21, 09:30 in Victoria. It is dry at home all day and it rains at the clinic for the hour
of an outing there, so the day at home and the hours of the outing differ and the request that was
made says which of them the morning line was given (ADR 0383). The places and the times are
invented.
"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.runtime import day_plan as runtime
from jarvis.runtime.home import Home
from jarvis.runtime.plan_run import PlanWatch
from jarvis.state import places, plan
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import emit_event, open_event_log, open_runtime_event_log
from jarvis.state.memory_db import open_memory_db
from tests.integration.weatherkit_world import PLACE, ZONE, Net, kit_for, make_key
from tests.integration.weatherkit_world import call as tool_call
from tests.support.home_routes import Connections

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric import ec

    from jarvis.runtime.plan_run import OpenLeg
    from jarvis.runtime.weatherkit import WeatherKit

DAY = date(2026, 10, 21)
LOCAL = ZoneInfo(ZONE)
SETTINGS = runtime.DayPlanSettings(at=time(4, 0), watch=True)
HOME_AT = "48.48,-123.38"
CLINIC = (48.43, -123.37)
CLINIC_AT = "48.43,-123.37"
LEG = {"from": "Home", "leave_at": "09:40", "line": "26", "board": "Main St",
       "arrive_at": "09:58", "backup": None}
DENTIST = plan.PlanItem(
    item_id="p9", title="Dentist", firmness="fixed", source="item:thing-c1", start="10:00",
    minutes=60, place="Clinic", route=LEG,
)
"""10:00 to 11:00 at the clinic, leaving at 09:40."""
CHECKUP = plan.PlanItem(
    item_id="p10", title="Check-up", firmness="fixed", source="item:thing-c2", start="15:00",
    minutes=60, place="Clinic",
    route={"from": "Home", "leave_at": "14:20", "line": "26", "board": "Main St",
           "arrive_at": "14:38", "backup": None},
)
"""15:00 to 16:00 at the clinic, leaving at 14:20."""


def _at(clock: str) -> datetime:
    return datetime.fromisoformat(f"{DAY.isoformat()}T{clock}:00").replace(tzinfo=LOCAL)


def _dry(_local: datetime) -> dict[str, Any]:
    return {"conditionCode": "Clear", "precipitationAmount": 0.0, "precipitationChance": 0.0,
            "temperature": 12.0}


def _rain_at(hour: int) -> Callable[[datetime], dict[str, Any]]:
    """The clinic's day: dry, and one hour of rain from ``hour`` o'clock."""

    def profile(local: datetime) -> dict[str, Any]:
        if local.hour == hour:
            return {"conditionCode": "Rain", "precipitationAmount": 1.0,
                    "precipitationChance": 0.9, "temperature": 9.0}
        return _dry(local)

    return profile


def _where(url: str) -> str:
    """``lat,lng`` of a WeatherKit request."""
    return ",".join(url.partition("?")[0].split("/")[-2:])


def _legs(end: dict[str, float] | None) -> OpenLeg:
    """A leg planner that answers as the real one does, its route ending at ``end`` if given."""

    @contextmanager
    def open_leg(_action_id: str) -> Iterator[Any]:
        def plan_leg(*_args: Any, **_kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
            option = {"leave_at": "09:45", "arrive_at": "09:58",
                      "legs": [{"route": "28", "board_stop": "Main St"}]}
            return {"chosen": option, "backup": None, **({"end_at": end} if end else {})}

        yield plan_leg

    return open_leg


@dataclass
class World:
    """The home, the watcher over its stores, and what its model was asked."""

    net: Net
    kit: WeatherKit
    home: Home
    watch: PlanWatch
    sources: runtime.DayPlanSources
    log: Path
    memory: Path
    opened: list[str]

    def drafted(self, *items: plan.PlanItem) -> None:
        """Draft today with ``items``; he woke at 07:00."""
        with closing(open_runtime_event_log(self.log)) as conn:
            plan.draft(conn, DAY, items, "d", "m")
            emit_event(
                conn, type="phone.state_observed",
                ts_epoch_ms=int(_at("07:00").timestamp() * 1000), ingestion_node="phone",
                payload={"signal": "focus", "value": "off"},
            )

    def keep(self, name: str, point: tuple[float, float]) -> None:
        """He keeps the place ``name`` at ``point``."""
        places.save(self.memory, name=name, lat=point[0], lng=point[1], origin="user")

    def tick(self, clock: str = "09:35") -> dict[str, Any]:
        """The morning line's input as the model saw it at ``clock``, when he is up.

        WeatherKit's client keeps the same time.
        """
        self.kit.clock = _at(clock).timestamp
        self.watch.tick(_at(clock))
        morning: dict[str, Any] = json.loads(self.opened[0])
        return morning


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey], *, open_leg: OpenLeg | None = None,
    **net: Any,  # noqa: ANN401 - the switches of the fake network
) -> World:
    fake = Net(monkeypatch, **net)
    kit = kit_for(key[0])
    home = Home(Connections(None), (ZONE, LOCAL), PLACE, weatherkit=kit)  # type: ignore[arg-type]
    log, memory = tmp_path / "events.db", tmp_path / "memory.db"
    open_event_log(log).close()
    open_memory_db(memory).close()
    sources = runtime.DayPlanSources(
        log, memory, LOCAL, DaySources(LOCAL, None, memory),
        weather=home.plan_weather, outing_weather=home.outing_weather,
    )
    opened: list[str] = []

    def morning(_system: str, user: str) -> str:
        opened.append(user)
        return json.dumps({"message": "Good morning."})

    watch = PlanWatch(settings=SETTINGS, sources=sources, ask=morning, open_leg=open_leg)
    return World(fake, kit, home, watch, sources, log, memory, opened)


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    """A throwaway signing key file and its key."""
    return make_key(tmp_path)


def test_an_outing_at_a_kept_place_is_given_that_places_hours_not_the_home_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Dry all day at home, rain at the clinic at 15:00: she is told the clinic's hours."""
    world = _world(
        tmp_path, monkeypatch, key, open_leg=_legs(None), hours=_dry,
        hours_at={CLINIC_AT: _rain_at(15)},
    )
    world.keep("Clinic", CLINIC)
    world.drafted(CHECKUP)
    seen = world.tick()
    expected = {
        "at": "Clinic", "from": "2:20 PM", "to": "4 PM", "max_chance_pct": 90, "rain_mm": 1.0,
        "low_c": 9.0, "high_c": 12.0, "likely_from": "3 PM", "likely_to": "4 PM",
        "hours": [
            {"at": "2 PM", "temp_c": 12.0, "condition": "clear", "rain_chance_pct": 0,
             "precip_mm": 0.0},
            {"at": "3 PM", "temp_c": 9.0, "condition": "rain", "rain_chance_pct": 90,
             "precip_mm": 1.0},
        ],
    }
    assert seen["outings"] == [{"item_id": "p10", "place": "Clinic", "weather": expected}]
    assert {_where(url) for url in world.net.weather()} == {HOME_AT, CLINIC_AT}


def test_the_draft_and_the_home_are_given_today_in_parts_from_one_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The home's line, the tool without days and the plan's weather are one WeatherKit read."""
    world = _world(tmp_path, monkeypatch, key, hours=_dry)
    assert world.home.weather() is not None
    tool_call(world.kit)
    inputs = runtime.gather(world.sources, SETTINGS, DAY, _at("04:00"))
    assert [_where(url) for url in world.net.weather()] == [HOME_AT]
    weather = inputs.weather
    assert weather is not None
    assert set(weather) == {"at", "now", "earlier_today", "rest_of_today", "tonight", "whole_day"}
    assert weather["at"] == "home"
    assert weather["earlier_today"] == {"rain_mm": 0.0, "note": "dry since midnight"}
    assert weather["rest_of_today"] == {
        "max_chance_pct": 0, "rain_mm": 0.0, "note": "no more rain expected today",
    }
    assert weather["tonight"]["from"] == "19:00"  # a clock alone is HH:MM in a plan
    assert weather["whole_day"]["sunrise"] == "07:30"
    assert "weather" not in inputs.notes


def test_another_day_is_its_row_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Parts exist for the day it is; another day has only what a day row says."""
    world = _world(tmp_path, monkeypatch, key)
    tomorrow = world.home.plan_weather(DAY + timedelta(days=1))
    assert tomorrow is not None
    assert set(tomorrow) == {"at", "whole_day"}
    assert tomorrow["whole_day"]["date"] == "2026-10-22"


def test_an_outing_whose_place_has_no_known_position_is_given_home_s_hours_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Not kept: the request is the home's, and the outing's weather says home."""
    world = _world(tmp_path, monkeypatch, key, open_leg=_legs(None), hours=_dry)
    world.drafted(DENTIST)
    seen = world.tick("08:00")
    weather = seen["outings"][0]["weather"]
    assert weather["at"] == "home"
    assert weather["why_home"] == "the position of Clinic is not known"
    assert weather["note"] == "no rain expected"  # the rain's own note is not the place's
    assert weather["max_chance_pct"] == 0
    assert {_where(url) for url in world.net.weather()} == {HOME_AT}


def test_weatherkit_down_leaves_a_plan_without_weather_and_still_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A failed read is a morning line without weather, never a failure."""
    world = _world(
        tmp_path, monkeypatch, key, open_leg=_legs({"lat": CLINIC[0], "lng": CLINIC[1]}),
        refuse=503,
    )
    world.keep("Clinic", CLINIC)
    world.drafted(DENTIST)
    seen = world.tick()
    assert seen["weather"] is None
    assert seen["outings"] == []
