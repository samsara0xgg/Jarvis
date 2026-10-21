"""The zone label a mail writes beside a time, and the instants it stands for (ADR 0379).

Behaviour tables over ``jarvis.shared.zone_label``. The dates they rely on, with the zone data
this machine has (``test_the_zone_data_gives_the_offsets_these_tables_rely_on`` fails first on a
machine whose data differs: BC and Alberta staying on summer time for good is in tzdata 2026c,
Manitoba in 2026e):

* 2026-07-15 and 2027-03-20: each US zone and its Canadian namesake agree, one reading;
* 2026-11-12 and 2027-03-04: US Pacific is UTC-8 and BC UTC-7, US Mountain UTC-7 and Alberta
  UTC-6, US Central UTC-6 and Manitoba UTC-5: two readings;
* Eastern rows use 2026-07-15 and 2026-11-12, when Toronto and New York agree.

His zone is America/Vancouver, so a reading is written on BC's clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jarvis.shared import lang, zone_label
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
    """The premise: each US zone and its Canadian namesake agree in summer, differ in winter."""
    keys = (
        "America/Los_Angeles",
        "America/Vancouver",
        "America/Edmonton",
        "America/Winnipeg",
        "America/Toronto",
        "America/Denver",
        "America/Chicago",
    )
    hours = {
        day: tuple(
            datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo(key)).utcoffset()
            for key in keys
        )
        for day in (SUMMER, WINTER, LATE_WINTER, SPRING)
    }
    h = timedelta
    summer = tuple(h(hours=n) for n in (-7, -7, -6, -5, -4, -6, -5))
    winter = tuple(h(hours=n) for n in (-8, -7, -6, -5, -5, -7, -6))
    assert hours[SUMMER] == summer
    assert hours[WINTER] == winter
    assert hours[LATE_WINTER] == winter
    assert hours[SPRING] == summer


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


@pytest.mark.parametrize("label", ["MST", "MDT", "MT", "Mountain", "Mountain Standard Time"])
def test_a_mountain_label_is_two_readings_while_us_mountain_and_alberta_differ(label: str) -> None:
    """9 am Mountain is 08:00 on BC's clock (Edmonton) or 09:00 on it (Denver); earlier first."""
    sentence = f"Thursday at 9 am {label}"
    assert _read(sentence, WINTER, (9, 0)) == ["2026-11-12T08:00-07:00", "2026-11-12T09:00-07:00"]
    assert _read(sentence, LATE_WINTER, (9, 0)) == [
        "2027-03-04T08:00-07:00",
        "2027-03-04T09:00-07:00",
    ]
    assert _read(sentence, SUMMER, (9, 0)) == ["2026-07-15T08:00-07:00"]
    assert _read(sentence, SPRING, (9, 0)) == ["2027-03-20T08:00-07:00"]


@pytest.mark.parametrize("label", ["CST", "CDT", "CT", "Central", "Central Time"])
def test_a_central_label_is_two_readings_while_us_central_and_manitoba_differ(label: str) -> None:
    """11 am Central is 09:00 on BC's clock (Winnipeg) or 10:00 on it (Chicago); earlier first."""
    sentence = f"Thursday at 11 am {label}"
    assert _read(sentence, WINTER, (11, 0)) == ["2026-11-12T09:00-07:00", "2026-11-12T10:00-07:00"]
    assert _read(sentence, LATE_WINTER, (11, 0)) == [
        "2027-03-04T09:00-07:00",
        "2027-03-04T10:00-07:00",
    ]
    assert _read(sentence, SUMMER, (11, 0)) == ["2026-07-15T09:00-07:00"]
    assert _read(sentence, SPRING, (11, 0)) == ["2027-03-20T09:00-07:00"]


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
    """Eastern, UTC and GMT are one zone each, and Central and Mountain one time in summer."""
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


@pytest.mark.parametrize(
    ("sentence", "quoted"),
    [
        ("Thursday, November 12 at 10 am PT", "10 am PT"),
        ("Friday 2:00 PM - 2:30 PM PT", "2:00 PM - 2:30 PM PT"),
        ("Thursday 10:00 (PST)", "10:00 PST"),
        ("At 10 am Pacific Standard Time on Thursday", "10 am Pacific Standard Time"),
        ("Thursday 10:00 UTC-5", "10:00 UTC-5"),
        ("Thursday 2pm ET", "2pm ET"),
        ("Thursday at 10 am", ""),
    ],
)
def test_said_quotes_the_time_and_label_the_mail_wrote(sentence: str, quoted: str) -> None:
    """The words shown beside the readings are the mail's own; nothing when it names no zone."""
    assert zone_label.said(sentence) == quoted


def test_when_text_is_the_time_alone_or_both_readings_through_the_table() -> None:
    """One reading is its clock (with the range's end); two are the table's sentence."""
    first = datetime(2026, 11, 12, 10, 0, tzinfo=DEFAULT_ZONE)
    later = datetime(2026, 11, 12, 11, 0, tzinfo=DEFAULT_ZONE)
    assert zone_label.when_text(first, None, DEFAULT_ZONE) == "10:00"
    assert zone_label.when_text(first, None, DEFAULT_ZONE, minutes=30) == "10:00-10:30"
    for language in ("en", "zh"):
        pacific = lang.t("job.zones.pacific", lang=language)
        assert zone_label.when_text(
            first, later, DEFAULT_ZONE, sentence="Thursday at 10 am PT", language=language
        ) == lang.t(
            "job.when.two", lang=language, first="10:00", later="11:00", said="10 am PT",
            zones=pacific,
        )
        assert zone_label.when_text(
            first,
            later,
            DEFAULT_ZONE,
            sentence="Thursday 10:00 - 10:30 PST",
            minutes=30,
            language=language,
        ) == lang.t(
            "job.when.two",
            lang=language,
            first="10:00-10:30",
            later="11:00-11:30",
            said="10:00 - 10:30 PST",
            zones=pacific,
        )


@pytest.mark.parametrize(
    ("sentence", "said", "zones"),
    [
        ("Thursday at 9 am MST", "9 am MST", "job.zones.mountain"),
        ("Thursday at 9 am Mountain Time", "9 am Mountain Time", "job.zones.mountain"),
        ("Thursday at 11 am CT", "11 am CT", "job.zones.central"),
        ("Thursday at 10 am Pacific", "10 am Pacific", "job.zones.pacific"),
    ],
)
def test_when_text_names_the_two_zones_the_label_can_mean(
    sentence: str, said: str, zones: str
) -> None:
    """Each label names its own pair: BC, Alberta or Manitoba beside its US namesake."""
    first = datetime(2026, 11, 12, 8, 0, tzinfo=DEFAULT_ZONE)
    later = datetime(2026, 11, 12, 9, 0, tzinfo=DEFAULT_ZONE)
    for language in ("en", "zh"):
        assert zone_label.when_text(
            first, later, DEFAULT_ZONE, sentence=sentence, language=language
        ) == lang.t(
            "job.when.two", lang=language, first="08:00", later="09:00", said=said,
            zones=lang.t(zones, lang=language),
        )
    assert zone_label.when_text(first, later, DEFAULT_ZONE, sentence=sentence, language="en") in {
        f"08:00 or 09:00 (the mail says {said}, BC or US Pacific)",
        f"08:00 or 09:00 (the mail says {said}, Alberta or US Mountain)",
        f"08:00 or 09:00 (the mail says {said}, Manitoba or US Central)",
    }


def test_when_text_names_no_zone_when_the_kept_sentence_has_lost_its_label() -> None:
    """A sentence kept cut at 200 characters may lose its label while its readings stand."""
    first = datetime(2026, 11, 12, 10, 0, tzinfo=DEFAULT_ZONE)
    later = datetime(2026, 11, 12, 11, 0, tzinfo=DEFAULT_ZONE)
    assert zone_label.when_text(
        first, later, DEFAULT_ZONE, sentence="Thursday, November 12 at 10 am", language="en"
    ) == "10:00 or 11:00 (US or Canadian time)"


def test_when_text_names_the_day_of_a_later_reading_on_another_day() -> None:
    """A later reading that falls on the next day says which, so it is not read as the same day."""
    first = datetime(2026, 11, 12, 23, 30, tzinfo=DEFAULT_ZONE)
    later = datetime(2026, 11, 13, 0, 30, tzinfo=DEFAULT_ZONE)
    assert zone_label.when_text(
        first, later, DEFAULT_ZONE, sentence="11:30 pm PST", language="en"
    ) == lang.t(
        "job.when.two", lang="en", first="23:30", later="11-13 00:30", said="11:30 pm PST",
        zones=lang.t("job.zones.pacific", lang="en"),
    )


def test_when_text_reads_the_readings_in_the_zone_it_is_given() -> None:
    """Instants from any zone are worded on the zone passed, never the one they came in."""
    zone = timezone(timedelta(hours=-7))
    first = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)
    assert zone_label.when_text(first, None, zone) == "10:00"
