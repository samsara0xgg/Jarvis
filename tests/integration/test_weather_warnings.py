"""ADR 0357: warnings, the coming shower and the last good answer, over a faked WeatherKit.

Alerts say whether a warning service exists and carry their words, a coming shower is judged by
its chance, and a failed read gives the last good answer. WeatherKit and the rest are faked over
``urlopen`` (``weatherkit_world``) with a throwaway signing key, so a request for the wrong place,
data set or alert reads wrong data here and no real service is called. Every alert, place and
time is invented.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.tools import ToolError
from jarvis.runtime.weatherkit import (
    ALERT_TEXT_CHARS,
    MAX_ALERTS,
    NO_SERVICE,
    NONE_IN_FORCE,
    NOT_ASKED,
    NOT_CHECKED,
    WeatherKit,
)
from tests.integration.weatherkit_world import (
    ALERT_ID,
    NOW,
    ZONE,
    Net,
    kit_for,
    make_key,
    rain_all_day,
)
from tests.integration.weatherkit_world import call as _call

if TYPE_CHECKING:
    from pathlib import Path

    from cryptography.hazmat.primitives.asymmetric import ec

WEATHER = "/api/v1/weather/en/48.48/-123.38"
AVAILABILITY = "/api/v1/availability/48.48/-123.38"
WHY = "Apple WeatherKit answered 503 SERVICE_UNAVAILABLE"
HOUR_S = 3600


@pytest.fixture
def key(tmp_path: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    """A throwaway signing key file and its key."""
    return make_key(tmp_path)


def _later(kit: WeatherKit) -> list[float]:
    """Make ``kit``'s clock one the test moves: ``now[0] += seconds``."""
    now = [NOW.timestamp()]
    kit.clock = lambda: now[0]
    return now


def _texts(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False)


# --- a warning service, or none ---------------------------------------------------------------


def test_an_alert_in_force_carries_its_text_trimmed_and_when_it_starts_and_ends(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The text is one line cut at a word; the times are the event's, in the place's own time."""
    net = Net(monkeypatch, warned=1)
    report = _call(kit_for(key[0]))
    (alert,) = report["alerts"]
    assert {k: alert[k] for k in ("headline", "severity", "area", "source")} == {
        "headline": "Rainfall Warning", "severity": "moderate", "area": "Greater Victoria",
        "source": "Environment Canada",
    }
    assert (alert["starts"], alert["ends"]) == ("today 11:30", "today 23:30")
    assert alert["text"].startswith(
        "Heavy rain is expected. Rainfall amounts of 50 to 70 mm are possible by Thursday morning.",
    )
    assert alert["text"].endswith("…")
    assert len(alert["text"]) <= ALERT_TEXT_CHARS + 1
    assert "\n" not in alert["text"]
    assert "  " not in alert["text"]
    assert "details_url" not in alert  # its own words are given, so the page is not
    # With one in force there is no need to ask what the place offers: its text is the one more.
    assert net.paths() == [WEATHER, f"/api/v1/weatherAlert/en/{ALERT_ID}0"]
    assert ALERT_ID not in _texts(report)  # an id is never in a result
    assert report["summary"].startswith("home: official warning in force: Rainfall Warning; ")


def test_an_alert_whose_text_cannot_be_read_keeps_its_headline_and_names_its_page(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The alert stands on the forecast's own row: its times, and the page for the rest."""
    Net(monkeypatch, warned=1, fail=("/api/v1/weatherAlert/",))
    (alert,) = _call(kit_for(key[0]))["alerts"]
    assert alert["headline"] == "Rainfall Warning"
    assert "text" not in alert
    assert alert["details_url"] == "https://weather.gc.ca/warnings"
    assert (alert["starts"], alert["ends"]) == ("today 09:30", "today 21:30")


def test_the_most_severe_alerts_are_given_in_full_up_to_three_and_the_rest_are_counted(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Five in force: three texts are read (three more requests), most severe first."""
    net = Net(monkeypatch, warned=5)
    report = _call(kit_for(key[0]))
    assert MAX_ALERTS == 3
    assert [a["headline"] for a in report["alerts"]] == [
        "Storm Surge Warning", "Wind Warning", "Rainfall Warning",  # extreme, severe, moderate
    ]
    assert [a["severity"] for a in report["alerts"]] == ["extreme", "severe", "moderate"]
    assert report["more_alerts"] == 2
    assert len([p for p in net.paths() if "weatherAlert" in p]) == MAX_ALERTS
    assert report["summary"].startswith(
        "home: 5 official warnings in force: Storm Surge Warning; Wind Warning;"
        " Rainfall Warning; 2 more; ",
    )


def test_warnings_on_a_day_of_rain_cost_hour_rows_before_they_cost_the_warning(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Rain every hour and five warnings: the hours thin out, the three texts and the line stay."""
    Net(monkeypatch, hours=rain_all_day, minutes="raining", warned=5)
    report = _call(kit_for(key[0]))
    assert len(_texts(report)) <= 3800
    assert report["left_out"] == {"next_hours": report["left_out"]["next_hours"]}
    assert 0 < report["left_out"]["next_hours"] < 12
    assert all(a["text"].endswith("\u2026") for a in report["alerts"])
    assert report["more_alerts"] == 2
    assert report["summary"].count("Warning") == 3


def test_none_in_force_says_so_where_a_service_covers_the_place_and_otherwise_that_none_does(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The same empty answer from WeatherKit is two facts; the data sets of the place tell which."""
    Net(monkeypatch, service=True)
    covered = _call(kit_for(key[0]))["alerts"]
    assert covered == NONE_IN_FORCE == "none in force"
    Net(monkeypatch, service=False)
    uncovered = _call(kit_for(key[0]))["alerts"]
    assert uncovered == NO_SERVICE == "no official warning service covers this place"


def test_the_data_sets_of_a_place_are_asked_once_a_day_for_each_point_to_two_decimals(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Another request for the same place, or a point a few metres off, does not ask again."""
    net = Net(monkeypatch)
    kit = kit_for(key[0])
    now = _later(kit)

    def asked() -> list[str]:
        return [p for p in net.paths() if "availability" in p]

    _call(kit)
    _call(kit, {"when": "tomorrow"})
    _call(kit, {"place": "48.481,-123.379"})  # 48.48, -123.38 to two decimals
    assert asked() == [AVAILABILITY]
    _call(kit, {"place": "48.5,-123.4"})  # another point
    assert asked() == [AVAILABILITY, "/api/v1/availability/48.5/-123.4"]
    now[0] += 24 * HOUR_S - 1
    _call(kit, {"when": "tomorrow"})
    assert len(asked()) == 2
    now[0] += 2
    _call(kit, {"when": "tomorrow"})
    assert asked()[-1] == AVAILABILITY  # a day later: asked again
    assert "country=CA" in next(one for one in net.asked if "availability" in one)


def test_a_failed_check_of_the_data_sets_leaves_alerts_unchecked_not_empty(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The forecast stands; what cannot be said is not said to be none."""
    Net(monkeypatch, fail=("/api/v1/availability/",))
    report = _call(kit_for(key[0]))
    assert report["alerts"] == NOT_CHECKED
    assert "whole_day" in report


def test_a_place_whose_country_is_not_known_is_not_asked_for_alerts_at_all(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """No country: no alerts data set, no country in the request, no availability request."""
    net = Net(monkeypatch)
    report = _call(
        kit_for(key[0]), home={"latitude": 48.48, "longitude": -123.38}, zone="Etc/UTC",
    )
    assert net.paths() == [WEATHER]
    assert "weatherAlerts" not in net.asked[0]
    assert "country" not in net.asked[0]
    assert report["alerts"] == NOT_ASKED


# --- a coming shower --------------------------------------------------------------------------


def _next_hour(net: Net, key: tuple[Path, ec.EllipticCurvePrivateKey]) -> dict[str, Any]:
    """The ``next_hour`` of the answer over ``net``, at 9:30."""
    found: dict[str, Any] = _call(kit_for(key[0]))["next_hour"]
    assert net.paths()[0] == WEATHER
    return found


def test_a_shower_starts_and_stops_where_the_summary_of_the_hour_says(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The summary's rain period, whatever the minutes under it show: 9:52 from a 70% period."""
    local = NOW.astimezone(ZoneInfo(ZONE)).replace(second=0)
    ahead = _next_hour(Net(monkeypatch), key)  # dry now, rain from minute 25 to 45
    assert ahead == {
        "now": "dry", "starts_at": (local + timedelta(minutes=25)).strftime("%H:%M"),
        "stops_at": None, "peak_mm_h": 1.5, "kind": "rain",
        "data_until": (local + timedelta(minutes=60)).strftime("%H:%M"),
    }
    raining = _next_hour(Net(monkeypatch, minutes="raining"), key)
    assert raining["now"] == "rain"
    assert raining["stops_at"] == (local + timedelta(minutes=20)).strftime("%H:%M")
    assert raining["starts_at"] is None
    led = _next_hour(Net(monkeypatch, minutes="summary-leads"), key)  # minutes begin at 9:54
    assert (led["now"], led["starts_at"]) == ("dry", "09:52")


@pytest.mark.parametrize("minutes", ["low-chance", "summary-unlikely", "dry"])
def test_a_low_chance_is_dry_with_a_summary_or_without(
    minutes: str, monkeypatch: pytest.MonkeyPatch,
    key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Intensity at a 20% chance, a summary period at 30%, or nothing: no start, no kind."""
    for summary in (True, False):
        found = _next_hour(Net(monkeypatch, minutes=minutes, summary=summary), key)
        assert (found["now"], found["starts_at"], found["stops_at"]) == ("dry", None, None)
        assert found["peak_mm_h"] == 0
        assert "kind" not in found


def test_without_a_summary_a_minute_counts_at_half_a_chance_and_some_intensity(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The minutes alone: 80% and 70% minutes rain, from the first of them."""
    chance = _next_hour(Net(monkeypatch, summary=False), key)
    assert (chance["now"], chance["starts_at"], chance["kind"]) == ("dry", "09:55", "rain")
    net = Net(monkeypatch, minutes="summary-leads", summary=False)
    assert _next_hour(net, key)["starts_at"] == "09:54"  # the summary is what makes it 9:52


def test_an_hour_with_no_minutes_is_a_plain_reason(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """No minute data: no guess from the summary alone."""
    found = _next_hour(Net(monkeypatch, minutes="none"), key)
    assert "no minute-by-minute precipitation" in found["unavailable"]


# --- the last good answer ---------------------------------------------------------------------


def _good_then_down(
    net: Net, key: tuple[Path, ec.EllipticCurvePrivateKey], args: dict[str, Any] | None = None,
) -> tuple[WeatherKit, list[float], dict[str, Any]]:
    """A good read at 9:30, then WeatherKit answering 503; the client, its clock and the read."""
    kit = kit_for(key[0])
    now = _later(kit)
    good = _call(kit, args)
    net.refuse, net.reason = 503, "SERVICE_UNAVAILABLE"
    return kit, now, good


def test_a_failed_read_gives_the_last_good_answer_with_its_time_and_why(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Two hours on, WeatherKit says 503: what was read at 9:30, said to be that."""
    kit, now, good = _good_then_down(Net(monkeypatch, warned=1), key)
    now[0] += 2 * HOUR_S
    held = _call(kit)
    assert list(held)[:2] == ["summary", "held"]
    assert held["held"] == {"read_at": "today 09:30", "age_min": 120, "why": WHY}
    assert held["summary"].startswith(
        f"As of today 09:30, the last good reading (a new one failed: {WHY}): home: ",
    )
    assert len(held["summary"]) <= 400
    for name in ("place", "now", "earlier_today", "rest_of_today", "tonight", "whole_day",
                 "tomorrow", "alerts", "attribution"):
        assert held[name] == good[name]
    assert len(_texts(held)) <= 4000
    assert "web_search" not in _texts(held)


def test_a_held_answer_is_for_the_same_request_only_and_for_six_hours(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Other days or other flags are another request; the answer is gone after six hours."""
    kit, now, _good = _good_then_down(Net(monkeypatch), key)
    now[0] += 301  # the five minutes a forecast is reused for
    for args in ({"when": "tomorrow"}, {"hourly": True}, {"air_quality": True}):
        with pytest.raises(ToolError, match="weather is unavailable") as caught:
            _call(kit, args)
        assert (caught.value.code, caught.value.kind) == ("network_error", "dont_retry")
        assert "web_search" not in str(caught.value)
    now[0] += 6 * HOUR_S - 301 - 1
    assert _call(kit)["held"]["age_min"] == 360  # a second short of six hours
    now[0] += 2
    with pytest.raises(ToolError, match="weather is unavailable"):
        _call(kit)


def test_with_nothing_held_the_error_says_the_weather_is_unavailable_and_asks_for_no_search(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A first read that fails: no held answer, and no web search offered in its place."""
    net = Net(monkeypatch, refuse=503)
    net.reason = "SERVICE_UNAVAILABLE"
    with pytest.raises(ToolError) as caught:
        _call(kit_for(key[0]))
    assert str(caught.value) == f"weather is unavailable: {WHY}"
    assert (caught.value.code, caught.value.kind) == ("network_error", "dont_retry")
    assert "search" not in str(caught.value)


def test_a_refused_key_is_answered_from_the_held_answer_too(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """The reason is Apple's own word and the status; the key is nowhere in it."""
    net = Net(monkeypatch)
    kit, now, _good = _good_then_down(net, key)
    net.refuse, net.reason = 401, "NOT_ENABLED"
    now[0] += HOUR_S
    held = _call(kit)
    assert held["held"]["why"] == "Apple WeatherKit refused the key (401 NOT_ENABLED)"
    assert net.headers[-1]["Authorization"].split(" ")[1] not in _texts(held)


def test_the_first_line_of_a_held_answer_keeps_its_figures_and_stays_one_short_line(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """A long line loses its last sentences, never half of one, to make room for the time."""
    net = Net(monkeypatch, hours=rain_all_day, warned=5)
    kit, now, good = _good_then_down(net, key)
    now[0] += HOUR_S
    held = _call(kit)
    assert len(good["summary"]) + len(held["summary"].split("): ", 1)[0]) > 400  # it is cut
    assert len(held["summary"]) <= 400
    assert held["summary"].endswith(".")
    assert held["summary"].startswith("As of today 09:30, the last good reading")
    assert "5 official warnings in force: Storm Surge Warning; Wind Warning;" in held["summary"]
    assert held["summary"].count(";") < good["summary"].count(";")
    assert len(_texts(held)) <= 4000


def test_a_held_answer_is_cut_again_to_leave_room_for_what_it_is_given_with(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, ec.EllipticCurvePrivateKey],
) -> None:
    """Three alerts and a day of hourly rows: the held copy fits, and keeps what was left out."""
    args = {"when": "tomorrow", "hourly": True}
    kit, now, good = _good_then_down(Net(monkeypatch, hours=rain_all_day, warned=3), key, args)
    now[0] += HOUR_S
    held = _call(kit, args)
    assert len(_texts(good)) <= 3800
    assert len(_texts(held)) <= 4000
    assert held["held"]["why"] == WHY
    assert len(held["hourly"]) <= len(good["hourly"])
    assert held["left_out"]["hourly"] >= good.get("left_out", {}).get("hourly", 0)
