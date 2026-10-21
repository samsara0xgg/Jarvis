"""The zone label a mail writes beside a time, and the instants it stands for (ADR 0368).

Behaviour tables over ``jarvis.shared.zone_label``. The dates they rely on, with the zone data
this machine has (``test_the_zone_data_gives_the_offsets_these_tables_rely_on`` fails first on a
machine whose data differs, since BC's move to UTC-7 for good is in newer data only):

* 2026-07-15 and 2027-03-20: US Pacific and BC are both UTC-7, one reading;
* 2026-11-12 and 2027-03-04: US Pacific is UTC-8 and BC UTC-7, two readings;
* Mountain and Central rows use 2026-07-15 alone, when every release of the data agrees
  (Edmonton and Winnipeg change their winter offsets in newer data); Eastern rows use it and
  2026-11-12, when Toronto is on EST in every release.

His zone is America/Vancouver, so a reading is written on BC's clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.shared import zone_label
from tests.support.clock import DEFAULT_ZONE

SUMMER = date(2026, 7, 15)
WINTER = date(2026, 11, 12)
LATE_WINTER = date(2027, 3, 4)
SPRING = date(2027, 3, 20)


def _read(
    sentence: str, day: date, clock: tuple[int, int], zone: ZoneInfo = DEFAULT_ZONE
) -> list[str]:
    return [at.isoformat(timespec="minutes") for at in zone_label.read(sentence, day, clock, zone)]


def test_the_zone_data_gives_the_offsets_these_tables_rely_on() -> None:
    """The premise: US and BC Pacific agree in summer 2026 and spring 2027, differ in between."""
    keys = (
        "America/Los_Angeles",
        "America/Vancouver",
        "America/Edmonton",
        "America/Winnipeg",
        "America/Toronto",
    )
    hours = {
        day: tuple(
            datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(key)).utcoffset()
            for key in keys
        )
        for day in (SUMMER, WINTER, LATE_WINTER, SPRING)
    }
    h = timedelta
    assert hours[SUMMER] == (h(hours=-7), h(hours=-7), h(hours=-6), h(hours=-5), h(hours=-4))
    assert hours[WINTER][:2] == (h(hours=-8), h(hours=-7))
    assert hours[WINTER][4] == h(hours=-5)
    assert hours[LATE_WINTER][:2] == (h(hours=-8), h(hours=-7))
    assert hours[SPRING][:2] == (h(hours=-7), h(hours=-7))


@pytest.mark.parametrize("label", ["PST", "PDT", "PT", "Pacific", "Pacific Standard Time"])
def test_a_pacific_label_is_two_readings_while_us_and_bc_pacific_differ(label: str) -> None:
    """10 am Pacific is 10:00 on BC's clock or 11:00 on it (10:00 in Los Angeles); earlier first."""
    sentence = f"Thursday at 10 am {label}"
    assert _read(sentence, WINTER, (10, 0)) == ["2026-11-12T10:00-07:00", "2026-11-12T11:00-07:00"]
    assert _read(sentence, LATE_WINTER, (10, 0)) == [
        "2027-03-04T10:00-07:00",
        "2027-03-04T11:00-07:00",
    ]
    assert _read(sentence, SUMMER, (10, 0)) == ["2026-07-15T10:00-07:00"]
    assert _read(sentence, SPRING, (10, 0)) == ["2027-03-20T10:00-07:00"]


def test_the_two_readings_are_on_his_own_clock_whatever_it_is() -> None:
    """In Tokyo the same mail is 03:00 or 04:00 the next day; the earlier instant still leads."""
    tokyo = ZoneInfo("Asia/Tokyo")
    assert _read("10 am PST", WINTER, (10, 0), tokyo) == [
        "2026-11-13T02:00+09:00",
        "2026-11-13T03:00+09:00",
    ]


def test_a_reading_that_crosses_midnight_carries_its_own_date() -> None:
    """11:30 pm on BC's clock is 00:30 the next day when the mail meant Los Angeles."""
    assert _read("11:30 pm PST", WINTER, (23, 30)) == [
        "2026-11-12T23:30-07:00",
        "2026-11-13T00:30-07:00",
    ]


@pytest.mark.parametrize(
    ("sentence", "day", "clock", "reading"),
    [
        ("2 pm ET", SUMMER, (14, 0), "2026-07-15T11:00-07:00"),
        ("2 pm EDT", SUMMER, (14, 0), "2026-07-15T11:00-07:00"),
        ("2 pm EST", WINTER, (14, 0), "2026-11-12T12:00-07:00"),
        ("2 pm Eastern Time", WINTER, (14, 0), "2026-11-12T12:00-07:00"),
        ("11 am CT", SUMMER, (11, 0), "2026-07-15T09:00-07:00"),
        ("11 am Central", SUMMER, (11, 0), "2026-07-15T09:00-07:00"),
        ("9 am MT", SUMMER, (9, 0), "2026-07-15T08:00-07:00"),
        ("9 am Mountain", SUMMER, (9, 0), "2026-07-15T08:00-07:00"),
        ("17:00 UTC", WINTER, (17, 0), "2026-11-12T10:00-07:00"),
        ("17:00 GMT", SUMMER, (17, 0), "2026-07-15T10:00-07:00"),
    ],
)
def test_the_other_labels_keep_their_one_zone(
    sentence: str, day: date, clock: tuple[int, int], reading: str
) -> None:
    """Eastern, Central, Mountain, UTC and GMT are one zone each, on that date's own offset."""
    assert _read(sentence, day, clock) == [reading]


@pytest.mark.parametrize(
    ("sentence", "clock", "reading"),
    [
        ("10:00 UTC-5", (10, 0), "2026-11-12T08:00-07:00"),
        ("10:00 UTC\u22125", (10, 0), "2026-11-12T08:00-07:00"),  # a real minus sign
        ("10:00 UTC \u2013 5", (10, 0), "2026-11-12T08:00-07:00"),  # an en dash and spaces
        ("18:00 GMT+8", (18, 0), "2026-11-12T03:00-07:00"),
        ("10:00 UTC+05:30", (10, 0), "2026-11-11T21:30-07:00"),
        ("10:00 UTC+0530", (10, 0), "2026-11-11T21:30-07:00"),
        ("10:00 UTC-08:00", (10, 0), "2026-11-12T11:00-07:00"),
    ],
)
def test_an_offset_is_that_offset(sentence: str, clock: tuple[int, int], reading: str) -> None:
    """``UTC-5``, ``GMT+8`` and ``UTC+05:30`` are the fixed offset they write, in any dash."""
    assert _read(sentence, WINTER, clock) == [reading]


def test_an_offset_beats_a_name_in_the_same_sentence() -> None:
    """``10 am PST (UTC-8)`` is one instant, the offset's, though PST alone is two."""
    assert _read("10 am PST (UTC-8)", WINTER, (10, 0)) == ["2026-11-12T11:00-07:00"]
    assert _read("10 am PST (UTC-7)", WINTER, (10, 0)) == ["2026-11-12T10:00-07:00"]


def test_a_time_with_no_label_is_his_own_clock() -> None:
    """No label, one reading, on the zone given (daylight saving included)."""
    assert _read("Thursday at 10 am", WINTER, (10, 0)) == ["2026-11-12T10:00-07:00"]
    tokyo = ZoneInfo("Asia/Tokyo")
    assert _read("Thursday at 10 am", WINTER, (10, 0), tokyo) == ["2026-11-12T10:00+09:00"]
    # A word that only contains a label's letters is no label.
    assert _read("Interview with Pat at 10 am, see PTO notes", WINTER, (10, 0)) == [
        "2026-11-12T10:00-07:00"
    ]


def test_a_clock_that_is_no_time_of_day_is_refused() -> None:
    """The reader never makes up a time: the caller reads a ValueError as no clear time."""
    for clock in ((24, 0), (10, 61), (-1, 0)):
        with pytest.raises(ValueError, match="must be in"):
            zone_label.read("10 am PST", WINTER, clock, DEFAULT_ZONE)
