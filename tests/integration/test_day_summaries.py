"""Daily conversation summaries (ADR 0142): the schedule, the gates and ``recall``.

Real memory.db, real Event Log, real ``DaySummarySchedule`` and ``recall`` through
the default registry; only the summariser's provider call is faked, by the scripted model,
which answers per day and records what it was asked.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

import pytest

from jarvis.runtime.day_summary import (
    DaySummarySchedule,
    DaySummarySettings,
)
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    append_day_summary,
    latest_day_summaries,
    open_memory_db,
    pending_days,
)
from tests.support.clock import FakeClock
from tests.support.daily_tools import DailyHarness
from tests.support.day_summaries import (
    GOOD,
    TODAY,
    asked_days,
    day_ago,
    day_summariser,
    insert_records,
    record_id,
    stamp_ago,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tests.support.model import ScriptedModel


def _stored(path: Path) -> dict[str, list[str]]:
    with closing(open_memory_db(path)) as conn:
        out: dict[str, list[str]] = {}
        for day, summary in conn.execute("SELECT day, summary FROM day_summaries ORDER BY rowid"):
            out.setdefault(day, []).append(summary)
    return out


def _schedule(
    tmp_path: Path, summariser: ScriptedModel, at: time = time(5, 0),
) -> DaySummarySchedule:
    log = tmp_path / "events.db"
    open_event_log(log).close()
    return DaySummarySchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DaySummarySettings(at=at, preset="p", prompt="summarise", max_chars=500),
        llm_config={},
        event_log_path=log,
        pricing_table={},
        clock=FakeClock(),
        client=summariser.as_llm_client(),
    )


def _run_once(schedule: DaySummarySchedule, now: datetime) -> dict[str, str] | None:
    """What the polling loop does on one check: due, then write."""
    today = schedule.due(now)
    return None if today is None else schedule.write(today)


def test_the_schedule_summarises_exactly_the_past_days_with_records_and_none_yet(
    tmp_path: Path,
) -> None:
    """Oldest first, today skipped, a day with a row skipped, a rejected day stores nothing."""
    path = tmp_path / "memory.db"
    insert_records(
        path,
        [
            (record_id(5, 1), stamp_ago(5, 9), "allen", "第五天"),
            (record_id(4, 1), stamp_ago(4, 9), "allen", "已有摘要的一天"),
            (record_id(3, 1), stamp_ago(3, 9), "allen", "第三天"),
            (record_id(3, 2), stamp_ago(3, 9, 1), "jarvis", "好的"),
            # 23:59:30 on day 1 local, written in UTC: its stored date may be day 0.
            (record_id(1, 1), stamp_ago(1, 23, 59, utc=True), "allen", "跨零点"),
            (record_id(0, 1), stamp_ago(0, 0, 1), "allen", "今天"),
        ],
        pad=True,
    )
    append_day_summary(
        path,
        day=day_ago(4).isoformat(),
        summary="old",
        model="m",
        record_count=1,
        input_chars=1,
        output_chars=3,
    )
    assert [d for d, _, _ in pending_days(path, TODAY)] == [
        day_ago(5).isoformat(),
        day_ago(3).isoformat(),
        day_ago(1).isoformat(),
    ]

    replies = {
        day_ago(5).isoformat(): f"## x\n{GOOD}\n[record_id={'f' * 32}]",
        day_ago(1).isoformat(): "## x\n### Topics\nno other headings",
    }
    summariser = day_summariser(replies)
    schedule = _schedule(tmp_path, summariser)

    assert _run_once(schedule, datetime.now().astimezone().replace(hour=4, minute=59)) is None
    outcomes = _run_once(schedule, datetime.now().astimezone().replace(hour=5, minute=0))
    assert outcomes is not None
    # A rejected answer is asked once more in the same run.
    d5, d3, d1 = (day_ago(i).isoformat() for i in (5, 3, 1))
    first_run = asked_days(summariser)
    assert first_run == [d5, d5, d3, d1, d1]
    assert {request.system for request in summariser.requests} == {"summarise"}
    assert outcomes[day_ago(5).isoformat()].startswith("rejected (cites record ids")
    assert outcomes[day_ago(1).isoformat()].startswith("rejected (missing headings")
    assert outcomes[day_ago(3).isoformat()].startswith("landed (2 records")
    stored = _stored(path)
    assert set(stored) == {day_ago(4).isoformat(), day_ago(3).isoformat()}
    assert stored[day_ago(4).isoformat()] == ["old"]
    assert stored[day_ago(3).isoformat()] == [f"## {d3}\n{GOOD}"]  # the scripted model's answer
    # Once per local date: no second run today, even though two days are still open.
    assert _run_once(schedule, datetime.now().astimezone().replace(hour=23)) is None

    # The next day's run retries what was rejected and leaves the landed day alone.
    replies.clear()
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    assert schedule.due(tomorrow.replace(hour=5, minute=1)) == TODAY + timedelta(days=1)
    schedule.write(TODAY + timedelta(days=1))
    assert asked_days(summariser)[len(first_run) :] == [d5, d1, TODAY.isoformat()]
    assert set(_stored(path)) == {day_ago(i).isoformat() for i in (5, 4, 3, 1, 0)}


def test_a_failing_call_does_not_stop_the_days_after_it(tmp_path: Path) -> None:
    """One day's provider error is logged; the next day still lands."""
    path = tmp_path / "memory.db"
    insert_records(
        path,
        [
            (record_id(3, 1), stamp_ago(3, 9), "allen", "a"),
            (record_id(2, 1), stamp_ago(2, 9), "allen", "b"),
        ],
        pad=True,
    )
    summariser = day_summariser({day_ago(3).isoformat(): TimeoutError("provider unreachable")})
    outcomes = _schedule(tmp_path, summariser).write(TODAY)
    assert outcomes[day_ago(3).isoformat()] == "failed"
    assert outcomes[day_ago(2).isoformat()].startswith("landed")
    assert set(_stored(path)) == {day_ago(2).isoformat()}


def test_a_short_day_is_kept_word_for_word_without_a_model_call(tmp_path: Path) -> None:
    """Under the threshold the day's own lines are its entry; the model is not asked."""
    path = tmp_path / "memory.db"
    insert_records(
        path,
        [
            (record_id(2, 1), stamp_ago(2, 9), "allen", "早"),
            (record_id(2, 2), stamp_ago(2, 9, 1), "jarvis", "早上好"),
            (record_id(2, 3), stamp_ago(2, 9, 2), "picture_note", "[Picture note]\n一碗面"),
        ],
    )
    summariser = day_summariser()
    outcomes = _schedule(tmp_path, summariser).write(TODAY)
    assert outcomes[day_ago(2).isoformat()].startswith("verbatim (3 records")
    assert summariser.requests == []
    entry = _stored(path)[day_ago(2).isoformat()][-1]
    assert entry.endswith(
        "09:00 user: 早\n09:01 assistant: 早上好\n09:02 picture note: [Picture note]\n一碗面",
    ), "a picture's note is neither of them speaking (ADR 0223)"


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
    insert_records(
        daily.memory,
        [
            (record_id(2, 1), stamp_ago(2, 9), "allen", "前天说的话"),
            (record_id(1, 1), stamp_ago(1, 9), "allen", "昨天说的话"),
            (record_id(1, 2), stamp_ago(1, 9, 1), "jarvis", "昨天的回答"),
        ],
    )
    for text in ("first try", "current"):
        append_day_summary(
            daily.memory,
            day=day_ago(1).isoformat(),
            summary=text,
            model="m",
            record_count=2,
            input_chars=2,
            output_chars=len(text),
        )
    day = day_ago(1).isoformat()

    one = daily.call("recall", {"from": day})
    assert one["summaries"] == [{"day": day, "summary": "current"}]
    assert [line.split(": ", 1)[1] for line in one["lines"]] == ["昨天说的话", "昨天的回答"]
    assert list(one).index("summaries") < list(one).index("lines")

    wide = daily.call("recall", {"from": day_ago(2).isoformat(), "to": TODAY.isoformat()})
    assert wide["summaries"] == [{"day": day, "summary": "current"}]  # the day before has none
    assert wide["total"] == 3
    assert daily.call("recall", {"from": TODAY.isoformat()})["summaries"] == []
    with closing(open_memory_db(daily.memory)) as conn:
        assert latest_day_summaries(conn, day, day) == [(day, "current")]


def test_a_restart_does_not_ask_again_for_a_day_already_tried(tmp_path: Path) -> None:
    """The log's summary calls since ``at`` today stand for the run a restart forgot."""
    path = tmp_path / "memory.db"
    insert_records(path, [(record_id(2, 1), stamp_ago(2, 9), "allen", "第二天")], pad=True)
    rejected = f"## x\n{GOOD}\n[record_id={'f' * 32}]"
    summariser = day_summariser({day_ago(2).isoformat(): rejected})
    # The log stamps its rows with the real clock, so the run happens now, after a midnight ``at``.
    now = datetime.now().astimezone()
    assert _run_once(_schedule(tmp_path, summariser, time.min), now) is not None
    assert asked_days(summariser) == [day_ago(2).isoformat()] * 2
    restarted = _schedule(tmp_path, summariser, time.min)
    assert restarted.due(now) is None, "tried after at: the restart waits"
    assert asked_days(summariser) == [day_ago(2).isoformat()] * 2
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert _schedule(fresh, day_summariser(), time.min).due(now) == now.date(), (
        "a log with no call today: a start still writes"
    )
