"""ADR 0356: his words for a day or a stretch, in both languages, read by one resolver.

A behaviour table: the words, the direction a tool reads them in, and the first and last day they
name when it is Wednesday 2026-10-21. Weeks run Monday to Sunday. No clock, no tool, no model.
"""

from __future__ import annotations

from datetime import date

import pytest

from jarvis.execution.speakable import resolve_when
from jarvis.execution.tools import ToolError

TODAY = date(2026, 10, 21)  # a Wednesday

AHEAD = [
    ("today", "2026-10-21", "2026-10-21"),
    ("今晚", "2026-10-21", "2026-10-21"),
    ("tonight", "2026-10-21", "2026-10-21"),
    ("tomorrow", "2026-10-22", "2026-10-22"),
    ("明天", "2026-10-22", "2026-10-22"),
    ("后天", "2026-10-23", "2026-10-23"),
    ("the day after", "2026-10-23", "2026-10-23"),
    ("yesterday", "2026-10-20", "2026-10-20"),
    ("前天", "2026-10-19", "2026-10-19"),
    ("周五", "2026-10-23", "2026-10-23"),
    ("星期五", "2026-10-23", "2026-10-23"),
    ("礼拜五", "2026-10-23", "2026-10-23"),
    ("Fri", "2026-10-23", "2026-10-23"),
    ("Friday", "2026-10-23", "2026-10-23"),
    ("周三", "2026-10-21", "2026-10-21"),  # today is a Wednesday and counts
    ("周二", "2026-10-27", "2026-10-27"),
    ("周日", "2026-10-25", "2026-10-25"),
    ("周天", "2026-10-25", "2026-10-25"),
    ("这周五", "2026-10-23", "2026-10-23"),
    ("this Friday", "2026-10-23", "2026-10-23"),
    ("下周五", "2026-10-30", "2026-10-30"),
    ("next Friday", "2026-10-30", "2026-10-30"),
    ("next Mon", "2026-10-26", "2026-10-26"),
    ("上周五", "2026-10-16", "2026-10-16"),
    ("last Friday", "2026-10-16", "2026-10-16"),
    ("这周", "2026-10-21", "2026-10-25"),  # the rest of the running week
    ("this week", "2026-10-21", "2026-10-25"),
    ("下周", "2026-10-26", "2026-11-01"),
    ("next week", "2026-10-26", "2026-11-01"),
    ("这个月", "2026-10-21", "2026-10-31"),
    ("this month", "2026-10-21", "2026-10-31"),
    ("下个月", "2026-11-01", "2026-11-30"),
    ("这周末", "2026-10-24", "2026-10-25"),
    ("周末", "2026-10-24", "2026-10-25"),
    ("this weekend", "2026-10-24", "2026-10-25"),
    ("下周末", "2026-10-31", "2026-11-01"),
    ("next weekend", "2026-10-31", "2026-11-01"),
    ("25号", "2026-10-25", "2026-10-25"),
    ("the 25th", "2026-10-25", "2026-10-25"),
    ("21号", "2026-10-21", "2026-10-21"),
    ("20号", "2026-11-20", "2026-11-20"),  # passed this month: next month's
    ("10月25日", "2026-10-25", "2026-10-25"),
    ("Oct 25", "2026-10-25", "2026-10-25"),
    ("October 25th", "2026-10-25", "2026-10-25"),
    ("25 Oct", "2026-10-25", "2026-10-25"),
    ("10月20日", "2027-10-20", "2027-10-20"),
    ("2026-10-25", "2026-10-25", "2026-10-25"),
    ("2026-10-25T09:00:00", "2026-10-25", "2026-10-25"),
    ("周五到周日", "2026-10-23", "2026-10-25"),
    ("Fri to Sun", "2026-10-23", "2026-10-25"),
    ("fri-sun", "2026-10-23", "2026-10-25"),
    ("Oct 25 - 27", "2026-10-25", "2026-10-27"),
    ("10月25日至27日", "2026-10-25", "2026-10-27"),
    ("2026-10-25 through 2026-10-27", "2026-10-25", "2026-10-27"),
    ("tomorrow to Sunday", "2026-10-22", "2026-10-25"),
    ("  Tomorrow. ", "2026-10-22", "2026-10-22"),
]

BACK = [
    ("yesterday", "2026-10-20", "2026-10-20"),
    ("周五", "2026-10-16", "2026-10-16"),  # the last Friday
    ("Friday", "2026-10-16", "2026-10-16"),
    ("周三", "2026-10-21", "2026-10-21"),  # today is a Wednesday and counts
    ("上周五", "2026-10-16", "2026-10-16"),
    ("这周一", "2026-10-19", "2026-10-19"),
    ("上周", "2026-10-12", "2026-10-18"),
    ("last week", "2026-10-12", "2026-10-18"),
    ("这周", "2026-10-19", "2026-10-21"),  # a running week ends today
    ("this week", "2026-10-19", "2026-10-21"),
    ("这个月", "2026-10-01", "2026-10-21"),
    ("上个月", "2026-09-01", "2026-09-30"),
    ("last month", "2026-09-01", "2026-09-30"),
    ("上周末", "2026-10-17", "2026-10-18"),
    ("周末", "2026-10-17", "2026-10-18"),  # the last weekend
    ("last weekend", "2026-10-17", "2026-10-18"),
    ("25号", "2026-09-25", "2026-09-25"),
    ("21号", "2026-10-21", "2026-10-21"),
    ("10月25日", "2025-10-25", "2025-10-25"),
    ("Oct 20", "2026-10-20", "2026-10-20"),
    ("最近3天", "2026-10-19", "2026-10-21"),
    ("最近三天", "2026-10-19", "2026-10-21"),
    ("past 7 days", "2026-10-15", "2026-10-21"),
    ("周一以来", "2026-10-19", "2026-10-21"),
    ("从10月10日起", "2026-10-10", "2026-10-21"),
    ("since Oct 10", "2026-10-10", "2026-10-21"),
    ("上周五到周日", "2026-10-16", "2026-10-18"),
    ("last Friday to Sunday", "2026-10-16", "2026-10-18"),
]


@pytest.mark.parametrize(("words", "first", "last"), AHEAD)
def test_ahead_words_resolve_to_days(words: str, first: str, last: str) -> None:
    """A weather or schedule day word: the next weekday, a running week's rest."""
    assert resolve_when(words, TODAY, back=False, tool="weather") == (
        date.fromisoformat(first), date.fromisoformat(last),
    )


@pytest.mark.parametrize(("words", "first", "last"), BACK)
def test_back_words_resolve_to_days(words: str, first: str, last: str) -> None:
    """A mail or jobs day word: the last weekday, a running week up to today, past N days."""
    assert resolve_when(words, TODAY, back=True, tool="mail") == (
        date.fromisoformat(first), date.fromisoformat(last),
    )


@pytest.mark.parametrize(
    ("words", "back"),
    [
        ("sometime soon", False),
        ("", False),
        ("2026-13-45", False),
        ("past 3 days", False),  # back only
        ("since Monday", False),
        ("32号", False),
        ("2026-10-25 to 2026-10-20", False),  # ends before it starts
    ],
)
def test_words_it_cannot_read_are_invalid_arguments(words: str, *, back: bool) -> None:
    """The error names the tool and the forms it reads."""
    with pytest.raises(ToolError) as raised:
        resolve_when(words, TODAY, back=back, tool="weather")
    assert raised.value.code == "invalid_arguments"
    assert "weather:" in str(raised.value)
