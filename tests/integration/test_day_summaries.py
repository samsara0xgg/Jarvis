"""Daily conversation summaries (ADR 0142): the schedule, the gates and ``recall``.

Real memory.db, real Event Log, real ``DaySummarySchedule`` and ``recall`` through
the default registry; only the summariser's provider call is faked, by a client that
answers per day and records what it was asked.
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.day_summary import REQUIRED_HEADINGS
from jarvis.decision.llm import ChatResult
from jarvis.runtime.day_summary import DaySummarySchedule, DaySummarySettings
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_day_summary,
    latest_day_summaries,
    open_memory_db,
    pending_days,
)
from tests.integration.test_daily_tools import DailyHarness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

TODAY = datetime.now().astimezone().date()
GOOD = "\n".join(f"{heading}\nnone" for heading in REQUIRED_HEADINGS)


def _day(ago: int) -> date:
    return TODAY - timedelta(days=ago)


def _rid(ago: int, index: int) -> str:
    return f"{ago:016x}{index:016x}"  # 32 hex digits: the shape of an event uid


def _stamp(ago: int, hour: int, minute: int = 0, *, utc: bool = False) -> str:
    moment = datetime.combine(_day(ago), time(hour, minute)).astimezone()
    return (moment.astimezone(UTC) if utc else moment).isoformat(timespec="seconds")


def _insert(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany("INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", rows)


def _stored(path: Path) -> dict[str, list[str]]:
    with closing(open_memory_db(path)) as conn:
        out: dict[str, list[str]] = {}
        for day, summary in conn.execute("SELECT day, summary FROM day_summaries ORDER BY rowid"):
            out.setdefault(day, []).append(summary)
    return out


class _Summariser:
    """Stands in for the day-summary preset's client; ``replies`` maps a day to its answer."""

    provider = "openai"
    model = "fake-day-summariser"

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.replies: dict[str, str] = {}
        self.systems: list[str] = []

    def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        **_kwargs: object,
    ) -> ChatResult:
        text = str(messages[0]["content"])
        day = re.match(r"\[Records of (\S+),", text).group(1)  # type: ignore[union-attr]
        self.asked.append(day)
        self.systems.append(system)
        return ChatResult(
            text=self.replies.get(day, f"## {day}\n{GOOD}"),
            tool_calls=(),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            raw={},
            model_used=self.model,
        )


def _schedule(tmp_path: Path, summariser: _Summariser) -> DaySummarySchedule:
    log = tmp_path / "events.db"
    open_event_log(log).close()
    schedule = DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=time(5, 0), preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
    )
    schedule._client = summariser  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return schedule


def _run_once(schedule: DaySummarySchedule, now: datetime) -> dict[str, str] | None:
    """What the polling loop does on one check: due, then write."""
    today = schedule.due(now)
    return None if today is None else schedule.write(today)


def test_the_schedule_summarises_exactly_the_past_days_with_records_and_none_yet(
    tmp_path: Path,
) -> None:
    """Oldest first, today skipped, a day with a row skipped, a rejected day stores nothing."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            (_rid(5, 1), _stamp(5, 9), "allen", "第五天"),
            (_rid(4, 1), _stamp(4, 9), "allen", "已有摘要的一天"),
            (_rid(3, 1), _stamp(3, 9), "allen", "第三天"),
            (_rid(3, 2), _stamp(3, 9, 1), "jarvis", "好的"),
            # 23:59:30 on day 1 local, written in UTC: its stored date may be day 0.
            (_rid(1, 1), _stamp(1, 23, 59, utc=True), "allen", "跨零点"),
            (_rid(0, 1), _stamp(0, 0, 1), "allen", "今天"),
        ],
    )
    append_day_summary(
        path,
        day=_day(4).isoformat(),
        summary="old",
        model="m",
        record_count=1,
        input_chars=1,
        output_chars=3,
    )
    assert [d for d, _, _ in pending_days(path, TODAY)] == [
        _day(5).isoformat(),
        _day(3).isoformat(),
        _day(1).isoformat(),
    ]

    summariser = _Summariser()
    summariser.replies[_day(5).isoformat()] = f"## x\n{GOOD}\n[record_id={'f' * 32}]"  # unknown id
    summariser.replies[_day(1).isoformat()] = "## x\n### Topics\nno other headings"
    schedule = _schedule(tmp_path, summariser)

    assert _run_once(schedule, datetime.now().astimezone().replace(hour=4, minute=59)) is None
    outcomes = _run_once(schedule, datetime.now().astimezone().replace(hour=5, minute=0))
    assert outcomes is not None
    # A rejected answer is asked once more in the same run.
    d5, d3, d1 = (_day(i).isoformat() for i in (5, 3, 1))
    assert summariser.asked == [d5, d5, d3, d1, d1]
    assert set(summariser.systems) == {"summarise"}
    assert outcomes[_day(5).isoformat()].startswith("rejected (cites record ids")
    assert outcomes[_day(1).isoformat()].startswith("rejected (missing headings")
    assert outcomes[_day(3).isoformat()].startswith("landed (2 records")
    stored = _stored(path)
    assert set(stored) == {_day(4).isoformat(), _day(3).isoformat()}
    assert stored[_day(4).isoformat()] == ["old"]
    # Once per local date: no second run today, even though two days are still open.
    assert _run_once(schedule, datetime.now().astimezone().replace(hour=23)) is None

    # The next day's run retries what was rejected and leaves the landed day alone.
    summariser.replies.clear()
    summariser.asked.clear()
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    assert schedule.due(tomorrow.replace(hour=5, minute=1)) == TODAY + timedelta(days=1)
    schedule.write(TODAY + timedelta(days=1))
    assert summariser.asked == [_day(5).isoformat(), _day(1).isoformat(), TODAY.isoformat()]
    assert set(_stored(path)) == {_day(i).isoformat() for i in (5, 4, 3, 1, 0)}


def test_a_failing_call_does_not_stop_the_days_after_it(tmp_path: Path) -> None:
    """One day's provider error is logged; the next day still lands."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [(_rid(3, 1), _stamp(3, 9), "allen", "a"), (_rid(2, 1), _stamp(2, 9), "allen", "b")],
    )
    summariser = _Summariser()
    real_chat = summariser.chat

    def flaky(*, messages: list[dict[str, Any]], system: str, **kwargs: object) -> ChatResult:
        if _day(3).isoformat() in str(messages[0]["content"]):
            msg = "provider unreachable"
            raise TimeoutError(msg)
        return real_chat(messages=messages, system=system, **kwargs)

    summariser.chat = flaky  # type: ignore[method-assign]
    outcomes = _schedule(tmp_path, summariser).write(TODAY)
    assert outcomes[_day(3).isoformat()] == "failed"
    assert outcomes[_day(2).isoformat()].startswith("landed")
    assert set(_stored(path)) == {_day(2).isoformat()}


def test_the_schedule_is_off_without_a_block_or_an_at() -> None:
    """Absent block or absent ``at`` is no schedule at all."""
    assert DaySummarySettings.from_config(None) is None
    assert DaySummarySettings.from_config({"preset": "p", "prompt": "x"}) is None
    on = DaySummarySettings.from_config({"at": "05:00", "preset": "p", "prompt": "x"})
    assert on is not None
    assert (on.at, on.max_chars) == (time(5, 0), 3000)


@pytest.fixture
def daily(tmp_path: Path) -> Iterator[DailyHarness]:
    """One isolated persisted runtime per check."""
    harness = DailyHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.fx.close()


def test_recall_returns_the_current_summary_of_each_day_before_the_lines(
    daily: DailyHarness,
) -> None:
    """The latest row of a day wins, a day without one is absent, today has none."""
    _insert(
        daily.memory,
        [
            (_rid(2, 1), _stamp(2, 9), "allen", "前天说的话"),
            (_rid(1, 1), _stamp(1, 9), "allen", "昨天说的话"),
            (_rid(1, 2), _stamp(1, 9, 1), "jarvis", "昨天的回答"),
        ],
    )
    for text in ("first try", "current"):
        append_day_summary(
            daily.memory,
            day=_day(1).isoformat(),
            summary=text,
            model="m",
            record_count=2,
            input_chars=2,
            output_chars=len(text),
        )
    day = _day(1).isoformat()

    one = daily.call("recall", {"from": day})
    assert one["summaries"] == [{"day": day, "summary": "current"}]
    assert [line.split(": ", 1)[1] for line in one["lines"]] == ["昨天说的话", "昨天的回答"]
    assert list(one).index("summaries") < list(one).index("lines")

    wide = daily.call("recall", {"from": _day(2).isoformat(), "to": TODAY.isoformat()})
    assert wide["summaries"] == [{"day": day, "summary": "current"}]  # the day before has none
    assert wide["total"] == 3
    assert daily.call("recall", {"from": TODAY.isoformat()})["summaries"] == []
    with closing(open_memory_db(daily.memory)) as conn:
        assert latest_day_summaries(conn, day, day) == [(day, "current")]
