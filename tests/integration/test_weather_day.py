"""ADR 0355: today in parts, added up from the hourly rows, and the first line written from them.

Tables over hand-made hourly rows: what fell since midnight, what is left of the day, tonight,
the words of the first line, and the cut of an answer that is too long. Nothing here reads a
clock: the tests pass the time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.runtime.weather_day import (
    LINE_CHARS,
    day_sentence,
    first_line,
    fit,
    horizon,
    hours_between,
    split,
)
from jarvis.runtime.weatherkit import words

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.runtime.weather_day import Hour

ZONE = ZoneInfo("America/Vancouver")
MIDNIGHT = datetime(2026, 10, 21, tzinfo=ZONE)
type Profile = Callable[[int], dict[str, Any]]
"""The fields of the hour ``n`` hours after the day's midnight."""


def _rows(profile: Profile, *, hours: int = 48, start: datetime = MIDNIGHT) -> list[Hour]:
    """Hourly rows from ``start``: a cold-then-warm day unless the profile says otherwise."""
    rows: list[Hour] = []
    for n in range(hours):
        at = (start.astimezone(UTC) + timedelta(hours=n)).astimezone(start.tzinfo)
        base = {
            "conditionCode": "Cloudy", "precipitationAmount": 0.0, "precipitationChance": 0.0,
            "temperature": 6.0 + abs(at.hour - 6) / 2,
        }
        rows.append((at, base | profile(n)))
    return rows


def _at(hour: int, minute: int = 35, *, day: int = 21) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=ZONE)


def drizzle_before_dawn(n: int) -> dict[str, Any]:
    """4:00 to 7:00 today: 1 mm of drizzle; everything after is dry."""
    amount = {4: 0.4, 5: 0.3, 6: 0.3}.get(n, 0.0)
    return {"conditionCode": "Drizzle", "precipitationAmount": amount,
            "precipitationChance": 0.9} if amount else {}


def afternoon_rain(n: int) -> dict[str, Any]:
    """Rain from 14:00 to 20:00 today at 70%: 0.8 mm an hour."""
    if 14 <= n < 20:
        return {"conditionCode": "Rain", "precipitationAmount": 0.8, "precipitationChance": 0.7}
    return {}


def raining_this_morning(n: int) -> dict[str, Any]:
    """Rain from 9:00 to 12:00 today at 80%."""
    if 9 <= n < 12:
        return {"conditionCode": "Rain", "precipitationAmount": 0.8, "precipitationChance": 0.8}
    return {}


def _parts(profile: Profile, now: datetime) -> dict[str, Any]:
    return split(_rows(profile), now, words)


def test_drizzle_before_dawn_is_over_and_the_rest_of_the_day_and_tonight_are_dry() -> None:
    """The morning of the audit: 1 mm fell until 7:00, nothing is left, the low is at 6:00."""
    parts = _parts(drizzle_before_dawn, _at(9))
    assert parts == {
        "earlier_today": {"rain_mm": 1.0, "from": "4:00", "until": "7:00", "condition": "drizzle"},
        "rest_of_today": {
            "max_chance_pct": 0, "rain_mm": 0.0, "note": "no more rain expected today",
        },
        "tonight": {
            "from": "19:00", "to": "7:00", "low_c": 6.0, "low_at": "6:00",
            "rain_chance_pct": 0, "rain_mm": 0.0, "note": "dry",
        },
    }


def test_just_after_midnight_nothing_has_fallen_yet_to_add_up() -> None:
    """The hour of 0:00 has not ended: no ``earlier_today``; the whole day is still ahead."""
    parts = _parts(afternoon_rain, _at(0, 20))
    assert "earlier_today" not in parts
    assert parts["rest_of_today"]["likely_from"] == "14:00"


def test_a_dry_morning_says_so_and_the_afternoon_rain_has_its_hours() -> None:
    """Dry since midnight; rain likely from 14:00 to 20:00, 4.8 mm, up to 70%."""
    parts = _parts(afternoon_rain, _at(9))
    assert parts["earlier_today"] == {"rain_mm": 0.0, "note": "dry since midnight"}
    assert parts["rest_of_today"] == {
        "max_chance_pct": 70, "rain_mm": 4.8, "likely_from": "14:00", "likely_to": "20:00",
    }
    assert parts["tonight"]["rain_chance_pct"] == 70  # the 19:00 hour is the night's
    assert parts["tonight"]["rain_mm"] == 0.8
    assert "note" not in parts["tonight"]


def test_rain_that_has_begun_is_likely_from_now_not_from_the_start_of_the_hour() -> None:
    """At 9:35 the 9:00 hour is the current one: rain likely now until 12:00."""
    parts = _parts(raining_this_morning, _at(9))
    assert parts["rest_of_today"]["likely_from"] == "now"
    assert parts["rest_of_today"]["likely_to"] == "12:00"
    assert parts["earlier_today"] == {"rain_mm": 0.0, "note": "dry since midnight"}


@pytest.mark.parametrize(
    ("now", "begins"),
    [
        pytest.param(_at(9), "19:00", id="morning: tonight is 19:00 to 7:00 tomorrow"),
        pytest.param(_at(18, 59), "19:00", id="one minute before it starts"),
        pytest.param(_at(19, 0), "now", id="at 19:00 it has begun"),
        pytest.param(_at(22, 10), "now", id="at night: from now"),
        pytest.param(_at(3, 20), "now", id="small hours: the rest of the night that began"),
    ],
)
def test_tonight_runs_from_19_to_7_and_from_now_once_it_has_begun(
    now: datetime, begins: str,
) -> None:
    """The night is Apple's overnight part, 19:00 to 07:00; the hours are those that are left."""
    night = _parts(lambda _n: {}, now)["tonight"]
    assert night["from"] == begins
    assert night["to"] == "7:00"
    assert night["low_at"] == "6:00"  # the profile is coldest at 6:00
    assert night["low_c"] == 6.0


def test_a_trace_of_rain_is_at_least_a_tenth_so_it_is_still_something_that_fell() -> None:
    """Three hours of 0.02 mm add to 0.06: shown as 0.1, never as a dry 0.0 with an end time."""
    def sprinkle(n: int) -> dict[str, Any]:
        return {"precipitationAmount": 0.02, "precipitationChance": 0.3} if 2 <= n < 5 else {}

    earlier = _parts(sprinkle, _at(9))["earlier_today"]
    assert earlier["rain_mm"] == 0.1
    assert (earlier["from"], earlier["until"]) == ("2:00", "5:00")


def test_the_day_the_clocks_go_back_has_twenty_five_hours_and_all_of_them_count() -> None:
    """New York on 2026-11-01: rain through the repeated 1:00 hour is added up once per row."""
    zone = ZoneInfo("America/New_York")
    start = datetime(2026, 11, 1, tzinfo=zone)
    def rain(n: int) -> dict[str, Any]:
        return {"precipitationAmount": 0.5, "precipitationChance": 0.9} if 1 <= n <= 3 else {}

    rows = _rows(rain, hours=30, start=start)
    parts = split(rows, datetime(2026, 11, 1, 10, 35, tzinfo=zone), words)
    assert [at.strftime("%H:%M %Z") for at, _ in rows[1:4]] == [
        "01:00 EDT", "01:00 EST", "02:00 EST",
    ]
    assert parts["earlier_today"]["rain_mm"] == 1.5
    assert (parts["earlier_today"]["from"], parts["earlier_today"]["until"]) == ("1:00", "3:00")
    assert len([h for h in rows if h[0].date() == start.date()]) == 25


def test_hours_between_takes_the_rows_that_overlap_the_window() -> None:
    """From 13:30 to 16:10 touches the hours of 13:00, 14:00, 15:00 and 16:00."""
    rows = _rows(lambda _n: {})
    window = hours_between(rows, _at(13, 30), _at(16, 10))
    assert [at.hour for at, _ in window] == [13, 14, 15, 16]
    assert hours_between(rows, _at(13, 0), _at(14, 0)) == [rows[13]]


@pytest.mark.parametrize(
    ("now", "until"),
    [
        pytest.param(_at(9), _at(7, 0, day=22), id="morning: through tonight"),
        pytest.param(_at(20), _at(8, 35, day=22), id="evening: 12 hours is later than 7:00"),
        pytest.param(_at(3), _at(15, 35), id="small hours: 12 hours is later than 7:00"),
    ],
)
def test_the_next_hours_reach_the_end_of_tonight_or_twelve_hours_if_that_is_later(
    now: datetime, until: datetime,
) -> None:
    """The hours given run to whichever is later."""
    assert horizon(now) == until


def _parts_for_line(profile: Profile, now: datetime) -> dict[str, Any]:
    return _parts(profile, now)


def test_the_first_line_is_the_one_the_audit_asked_for() -> None:
    """The line of 2026-10-21 09:35 with the drizzle before dawn."""
    now = {"temp_c": 9.4, "condition": "cloudy"}
    line = first_line("Victoria", now, _parts_for_line(drizzle_before_dawn, _at(9)), [])
    assert line == (
        "Victoria: 9° and cloudy now; about 1 mm fell before 7:00; no more rain today;"
        " tonight's low 6° at 6:00, dry."
    )


def _tomorrow() -> dict[str, Any]:
    return {
        "day": "tomorrow (Thu)", "date": "2026-10-22", "high_c": 16.0, "low_c": 9.0,
        "condition": "rain", "rain_chance_pct": 70, "precip_mm": 3.5,
    }


@pytest.mark.parametrize(
    ("profile", "now", "sentence"),
    [
        pytest.param(
            afternoon_rain, _at(9), "rain likely 14:00-20:00 today (up to 70%, about 4.8 mm)",
            id="rain ahead has its hours, chance and amount",
        ),
        pytest.param(
            raining_this_morning, _at(9), "rain likely until 12:00 today (up to 80%, about 2.4 mm)",
            id="rain now runs until",
        ),
        pytest.param(
            lambda _n: {"precipitationChance": 0.4, "precipitationAmount": 0.1}, _at(9),
            "no more rain likely today (up to 40%)", id="a middling chance is not no rain",
        ),
        pytest.param(
            lambda _n: {"precipitationChance": 0.1}, _at(9), "no more rain today",
            id="a tenth is dry",
        ),
    ],
)
def test_the_rest_of_the_day_is_worded_by_its_chance(
    profile: Profile, now: datetime, sentence: str,
) -> None:
    """Likely, middling, or none: the sentence says which, never more than the figures."""
    line = first_line("home", None, _parts(profile, now), [])
    assert f"; {sentence}" in line or line.startswith(f"home: {sentence}")


@pytest.mark.parametrize(
    ("chance", "mm", "said"),
    [
        (0.7, 0.8, "tonight's low 6° at 6:00, rain likely (70%)"),
        (0.3, 0.2, "tonight's low 6° at 6:00, a 30% chance of rain"),
        (0.1, 0.0, "tonight's low 6° at 6:00, dry"),
    ],
)
def test_tonight_is_dry_only_when_the_chance_is_a_tenth_or_less(
    chance: float, mm: float, said: str,
) -> None:
    """A night with a 30% chance is not dry."""
    parts = _parts(lambda _n: {"precipitationChance": chance, "precipitationAmount": mm}, _at(9))
    assert said in first_line("home", None, parts, [])


def test_the_days_after_follow_in_order_and_a_past_day_is_what_fell() -> None:
    """Tomorrow is worded by its chance; yesterday by what fell; neither by a chance gone by."""
    yesterday = {
        "day": "yesterday (Tue)", "date": "2026-10-20", "high_c": 9.4, "low_c": 5.2,
        "condition": "rain", "rain_chance_pct": 100, "precip_mm": 8.0,
    }
    assert day_sentence(yesterday, past=True) == "yesterday (Tue): rain, 9°/5°, about 8 mm of rain"
    assert day_sentence(_tomorrow(), past=False) == (
        "tomorrow (Thu): rain, 16°/9°, rain 70% (about 3.5 mm)"
    )
    dry = {**_tomorrow(), "rain_chance_pct": 5, "precip_mm": 0.0, "condition": "sunny"}
    assert day_sentence(dry, past=False) == "tomorrow (Thu): sunny, 16°/9°, dry"
    line = first_line("home", None, {}, [(yesterday, True), (_tomorrow(), False)])
    assert line == (
        "home: yesterday (Tue): rain, 9°/5°, about 8 mm of rain;"
        " tomorrow (Thu): rain, 16°/9°, rain 70% (about 3.5 mm)."
    )


def test_a_line_for_many_days_keeps_whole_sentences_and_counts_the_rest() -> None:
    """Under the standard's 400 characters, the later days go and say how many."""
    days = [({**_tomorrow(), "day": f"Day {n}"}, False) for n in range(12)]
    line = first_line("Victoria, British Columbia, Canada", None, {}, days)
    assert len(line) <= LINE_CHARS
    assert line.startswith("Victoria, British Columbia, Canada: Day 0: rain")
    assert line.endswith(" more in the rows.")
    shown = line.count("Day ")
    assert f"{12 - shown} more in the rows" in line


def _answer(hours: int, days: int, air: int = 0) -> dict[str, Any]:
    return {
        "summary": "s", "days": [{"day": f"d{n}", "pad": "x" * 100} for n in range(days)],
        "hourly": [{"at": f"h{n}", "pad": "y" * 90} for n in range(hours)],
        "air_quality": {"days": [{"date": f"a{n}", "pad": "z" * 60} for n in range(air)]},
    }


def test_an_answer_that_fits_is_left_alone() -> None:
    """Nothing is left out and nothing is added."""
    answer = _answer(10, 2)
    assert fit(dict(answer), 4000) == answer


def test_an_answer_too_long_gives_up_the_hours_from_the_end_and_says_so() -> None:
    """Whole rows, the last first; the count and how to get them come with it."""
    answer = fit(_answer(30, 2), 2000)
    assert len(json.dumps(answer, ensure_ascii=False)) <= 2000
    kept = len(answer["hourly"])
    assert [row["at"] for row in answer["hourly"]] == [f"h{n}" for n in range(kept)]
    assert answer["left_out"] == {"hourly": 30 - kept}
    assert "narrower `when`" in answer["see_more"]
    assert len(answer["days"]) == 2


def test_the_hours_go_before_the_airs_days_and_those_before_the_days() -> None:
    """The order of giving things up, and the last day always stays."""
    answer = fit(_answer(5, 6, air=6), 1300)
    assert len(json.dumps(answer, ensure_ascii=False)) <= 1300
    assert answer["hourly"] == []
    assert answer["left_out"]["hourly"] == 5
    assert len(answer["air_quality"]["days"]) < 6
    answer = fit(_answer(0, 6), 100)
    assert len(answer["days"]) == 1
    assert answer["left_out"] == {"days": 5}
