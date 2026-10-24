"""ADR 0293, ADR 0285, ADR 0199: the day line carries his body and the sky.

Acceptance checks against the real fold, the real event log and the real WeatherKit code path
(only the network faked):

- a sleep block carries its stage samples (tested with the table of ``test_day_line``);
- a Watch workout is a ``workout`` item with its activity and what Health knew of it, once per
  start time;
- ``summary`` is the day's steps (a total reported twice once), resting heart rate and mean SDNN,
  each ``None`` when unreported, a sample counted on the day it starts, one reported late counted;
- ``weather`` is the day's card from WeatherKit's daily row, asked once for a day however often
  the day is read, ``None`` (never an error) for a day it does not cover, without a key, or when
  Apple fails, and a failure is not asked again at once.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.runtime import home as home_module
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.home import Home
from jarvis.state import phone_day
from jarvis.state.day_line import DaySources, day_window, fold_day, parse_day
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.weatherkit_world import Net, kit_for, make_key
from tests.support.day_line import HEALTH, VANCOUVER, Row, load_rows, minutes_ms

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

ZONE = ZoneInfo(VANCOUVER)
MINUTE = 60_000
HOUR = 60 * MINUTE
UUID = "0A1B2C3D-0000-4000-8000-0000000000AD"


def _workout(
    uid: str, start: float, end: float, detail: str | None = "running",
    meta: dict[str, Any] | None = None,
) -> Row:
    payload: dict[str, Any] = {
        "metric": "workout", "start_ms": start, "end_ms": end, "value": (end - start) * 60,
        "unit": "s",
    }
    if detail is not None:
        payload["detail"] = detail
    if meta is not None:
        payload["meta"] = meta
    return Row(HEALTH, uid, payload, end)


def test_a_workout_is_an_item_with_what_health_knew_of_it(tmp_path: Path) -> None:
    """Activity, energy, distance and average heart rate where known; reported twice is once."""
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    load_rows(conn, [
        _workout("w1", 600, 645, meta={
            "total_energy_kcal": 412.5, "distance_m": 8000.0, "avg_heart_rate": 151.5,
            "indoor": False, "activity_type": 37,
        }),
        _workout("w1-again", 600, 645, meta={"total_energy_kcal": 420.0}),  # the later row stands
        _workout("w2", 900, 930, detail=None),  # no name, no meta
        _workout("w3", 1000, 1030, "yoga", meta={"avg_heart_rate": "n/a"}),  # junk is left out
    ], start_ms)

    items = [
        i
        for i in fold_day(
            conn, start_ms, end_ms, minutes_ms(start_ms, 1200), DaySources(ZONE)
        ).items
        if i["kind"] == "workout"
    ]
    conn.close()

    assert items == [
        {
            "id": "w1",
            "lane": "you",
            "kind": "workout",
            "state": "done",
            "start_ms": minutes_ms(start_ms, 600),
            "end_ms": minutes_ms(start_ms, 645),
            "activity": "running",
            "text": "running",
            "energy_kcal": 420,
        },
        {
            "id": "w2",
            "lane": "you",
            "kind": "workout",
            "state": "done",
            "start_ms": minutes_ms(start_ms, 900),
            "end_ms": minutes_ms(start_ms, 930),
            "activity": "workout",
            "text": "workout",
        },
        {
            "id": "w3",
            "lane": "you",
            "kind": "workout",
            "state": "done",
            "start_ms": minutes_ms(start_ms, 1000),
            "end_ms": minutes_ms(start_ms, 1030),
            "activity": "yoga",
            "text": "yoga",
        },
    ]


def test_a_workout_with_all_its_meta_says_all_of_it(tmp_path: Path) -> None:
    """The three figures, as Health's own numbers."""
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    load_rows(
        conn,
        [
            _workout(
                "w1",
                600,
                645,
                meta={
                    "total_energy_kcal": 412.5,
                    "distance_m": 8000.0,
                    "avg_heart_rate": 151.5,
                },
            )
        ],
        start_ms,
    )
    (item,) = [
        i
        for i in fold_day(
            conn, start_ms, end_ms, minutes_ms(start_ms, 1200), DaySources(ZONE)
        ).items
        if i["kind"] == "workout"
    ]
    conn.close()
    assert (item["energy_kcal"], item["distance_m"], item["avg_hr"]) == (412.5, 8000, 151.5)


def _measure(conn: Any, metric: str, at_ms: int, value: float, *, ts: int | None = None) -> None:  # noqa: ANN401
    emit_event(
        conn,
        type="phone.measure_observed",
        ingestion_node="phone",
        ts_epoch_ms=at_ms if ts is None else ts,
        event_uid=f"{metric}-{at_ms}-{value}",
        payload={
            "metric": metric,
            "start_ms": at_ms,
            "end_ms": at_ms,
            "value": value,
            "unit": "ms",
            "sample_uuid": UUID,
        },
    )


def _total(conn: Any, at_ms: int, value: float, uid: str) -> None:  # noqa: ANN401
    emit_event(
        conn,
        type="phone.health_observed",
        ingestion_node="phone",
        ts_epoch_ms=at_ms + HOUR,
        event_uid=uid,
        payload={"metric": "steps", "start_ms": at_ms, "end_ms": at_ms + HOUR, "value": value},
    )


def test_the_summary_is_the_days_steps_resting_heart_rate_and_mean_sdnn(tmp_path: Path) -> None:
    """Counted on the day a sample starts; a total reported again once; late reports counted."""
    start_ms, end_ms = day_window(parse_day("2026-10-10"), ZONE)
    conn = open_event_log(tmp_path / "events.db")
    _total(conn, start_ms + 8 * HOUR, 1000, "s8")
    _total(conn, start_ms + 9 * HOUR, 800, "s9")
    _total(conn, start_ms + 9 * HOUR, 900, "s9-again")  # 9:00 grew to 900
    _total(conn, start_ms + 23 * HOUR, 100.4, "s23")  # reported at midnight sharp, still the day
    _total(conn, start_ms - HOUR, 7000, "yesterday")
    _total(conn, end_ms, 5000, "tomorrow")
    _measure(conn, "resting_heart_rate", start_ms + HOUR, 58)
    _measure(conn, "resting_heart_rate", start_ms + 20 * HOUR, 55.55)  # the latest one stands
    _measure(conn, "resting_heart_rate", start_ms - HOUR, 70)
    for n, sdnn in enumerate((40.0, 50.0, 61.0)):
        _measure(conn, "heart_rate_variability_sdnn", start_ms + (10 + n) * HOUR, sdnn)
    _measure(conn, "heart_rate_variability_sdnn", start_ms + 22 * HOUR, 80.0, ts=end_ms + 3 * HOUR)
    _measure(conn, "heart_rate_variability_sdnn", end_ms + HOUR, 999.0)  # tomorrow's
    _measure(conn, "heart_rate", start_ms + 12 * HOUR, 64.0)  # not a summary metric

    assert phone_day.day_summary(conn, start_ms, end_ms) == {
        "steps": 2000,
        "resting_hr": 55.5,
        "hrv": 57.8,
    }
    empty = phone_day.day_summary(conn, *day_window(date(2026, 10, 3), ZONE))
    conn.close()
    assert empty == {"steps": None, "resting_hr": None, "hrv": None}


def test_the_day_route_carries_the_summary_and_the_weather(tmp_path: Path) -> None:
    """Both keys are on the payload, and the weather reader is asked for the day served."""
    start_ms, _ = day_window(parse_day("2026-10-10"), ZONE)
    log = tmp_path / "events.db"
    conn = open_event_log(log)
    _total(conn, start_ms + 8 * HOUR, 1000, "s8")
    conn.close()
    asked: list[date] = []

    def weather(day: date) -> dict[str, Any] | None:
        asked.append(day)
        return {"condition": "rain", "high": 12.0, "low": 6.0, "sunrise_ms": 1, "sunset_ms": 2}

    day, now = date(2026, 10, 10), start_ms + 20 * HOUR
    body = read_day(log, ZONE, None, None, day, now, weather=weather)
    plain = read_day(log, ZONE, None, None, day, now)

    assert body["summary"] == {"steps": 1000, "resting_hr": None, "hrv": None}
    assert body["weather"] == {
        "condition": "rain",
        "high": 12.0,
        "low": 6.0,
        "sunrise_ms": 1,
        "sunset_ms": 2,
    }
    assert asked == [date(2026, 10, 10)]
    assert plain["weather"] is None


# --- the weather card ---------------------------------------------------------------------


def _home(tmp_path: Path, *, keyed: bool = True) -> Home:
    kit = None
    if keyed:
        path, _ = make_key(tmp_path)
        kit = kit_for(path)
        kit.clock = time.time
    return Home(
        object(),  # type: ignore[arg-type]
        (VANCOUVER, ZONE),
        {"latitude": 49.28, "longitude": -123.12, "country_code": "CA", "name": "Vancouver"},
        weatherkit=kit,
    )


def _today() -> date:
    return datetime.now(ZONE).date()


def test_the_weather_card_is_the_days_daily_row_asked_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Condition, high, low and sun times from the WeatherKit row; a second read is the first."""
    net = Net(monkeypatch)
    home = _home(tmp_path)
    day = _today()
    sunrise = datetime.combine(day, datetime.min.time(), ZONE) + timedelta(hours=7, minutes=30)
    sunset = datetime.combine(day, datetime.min.time(), ZONE) + timedelta(hours=19)

    card = home.day_weather_card(day)
    again = home.day_weather_card(day)

    assert card == {
        "condition": "rain",
        "high": 15.0,
        "low": 8.0,
        "sunrise_ms": int(sunrise.timestamp() * 1000),
        "sunset_ms": int(sunset.timestamp() * 1000),
    }
    assert again == card
    assert len([url for url in net.asked if "weatherkit" in url]) == 1


def test_a_day_weatherkit_does_not_cover_is_none_and_asks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Eight days back and ten ahead are out of range: None, no request."""
    net = Net(monkeypatch)
    home = _home(tmp_path)

    assert home.day_weather_card(_today() - timedelta(days=8)) is None
    assert home.day_weather_card(_today() + timedelta(days=10)) is None
    assert net.asked == []


def test_without_a_key_or_when_apple_fails_the_card_is_none_and_a_failure_is_not_retried_at_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No key: None. Apple down: None, logged by type only, asked again after the retry time."""
    assert _home(tmp_path / "unused", keyed=False).day_weather_card(_today()) is None
    (tmp_path / "k").mkdir()
    net = Net(monkeypatch, down=("weatherkit.apple.com",))
    home = _home(tmp_path / "k")
    day = _today()
    caplog.set_level("WARNING", logger="jarvis.runtime.home")

    assert home.day_weather_card(day) is None
    assert home.day_weather_card(day) is None
    assert len(net.asked) == 1
    assert [r.getMessage() for r in caplog.records if "day weather" in r.getMessage()] == [
        f"day weather: {day} not read (Unavailable)",
    ]

    later = time.time() + home_module.WEATHER_CARD_RETRY_S + 1
    monkeypatch.setattr("jarvis.runtime.home._clock.time", lambda: later)
    assert home.day_weather_card(day) is None
    assert len(net.asked) == 2
