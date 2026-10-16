"""ADR 0276 — the nightly run, its schedule and the plain-text report, over real files."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, time, timedelta
from typing import TYPE_CHECKING, Any

from jarvis.runtime import experience_report
from jarvis.runtime.day_experience import (
    DayExperienceSchedule,
    DayExperienceSettings,
    run_day_experience,
)
from jarvis.state.day_experience import compute_day
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import (
    MemorySettings,
    experience_days,
    latest_day_experience,
    open_memory_db,
    put_day_experience,
)
from tests.integration.test_day_experience import DAY, _log, _noon

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_the_nightly_run_backfills_missing_past_days_once_and_never_today(tmp_path: Path) -> None:
    """From the first turn to yesterday, a row per date; a second run writes nothing more."""
    log, memory = tmp_path / "events.db", tmp_path / "memory.db"
    today = DAY + timedelta(days=3)
    _log(log, [
        ("utterance.received", _noon(DAY), {"transcript": "hi", "turn_id": "T1"}, "mac"),
        ("surface.playback_started", _noon(DAY, 1), {
            "session_id": "s", "response_id": "r", "turn_id": "T1", "playback_generation_id": "g",
            "phase": "final", "channel": "speech", "speech_text_hash": "h"}, "mac"),
        ("surface.user_intent", _noon(DAY + timedelta(days=2)),
         {"transcript": "typed", "turn_id": "T2", "channel": "cli_stdin"}, "phone"),
        ("utterance.received", _noon(today), {"transcript": "now", "turn_id": "T3"}, "mac"),
    ])
    put_day_experience(memory, (DAY + timedelta(days=1)).isoformat(), json.dumps({"kept": True}))
    assert run_day_experience(memory, log, today) == {
        DAY.isoformat(): "written", (DAY + timedelta(days=2)).isoformat(): "written",
    }
    assert experience_days(memory) == {(DAY + timedelta(days=n)).isoformat() for n in range(3)}
    with closing(open_memory_db(memory)) as conn:
        stored = {d: json.loads(m) for d, m in latest_day_experience(conn, 10)}
    assert stored[(DAY + timedelta(days=1)).isoformat()] == {"kept": True}  # not recomputed
    first = stored[DAY.isoformat()]
    assert (first["uses"]["total"], first["interrupted_ratio"]["started"]) == (1, 1)
    last = stored[(DAY + timedelta(days=2)).isoformat()]
    assert last["uses"]["by_device"] == {"phone": {"spoken": 0, "typed": 1}}
    assert run_day_experience(memory, log, today) == {}
    assert run_day_experience(tmp_path / "fresh.db", log, DAY) == {}  # nothing before day one


def test_a_log_without_turns_writes_nothing(tmp_path: Path) -> None:
    """No first day, no rows."""
    log = tmp_path / "events.db"
    open_event_log(log).close()
    assert run_day_experience(tmp_path / "memory.db", log, DAY) == {}


def test_a_day_that_fails_is_left_for_the_next_run_and_the_days_after_it_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure is reported, nothing is stored for the day, and the next date is computed."""
    log, memory = tmp_path / "events.db", tmp_path / "memory.db"
    _log(log, [("utterance.received", _noon(DAY), {"transcript": "hi", "turn_id": "T1"}, "mac")])
    def flaky(conn: sqlite3.Connection, memory_db: Path, day: date) -> dict[str, Any]:
        if day == DAY:
            raise sqlite3.OperationalError
        return compute_day(conn, memory_db, day)

    monkeypatch.setattr("jarvis.runtime.day_experience.compute_day", flaky)
    outcomes = run_day_experience(memory, log, DAY + timedelta(days=3))
    assert outcomes == {
        DAY.isoformat(): "failed",
        (DAY + timedelta(days=1)).isoformat(): "written",
        (DAY + timedelta(days=2)).isoformat(): "written",
    }
    assert DAY.isoformat() not in experience_days(memory)


def test_settings_default_to_four_in_the_morning_and_can_be_switched_off() -> None:
    """A missing block or key is 04:00; false turns it off; an unreadable time turns it off."""
    assert DayExperienceSettings.from_config(None) == DayExperienceSettings(time(4, 0))
    assert DayExperienceSettings.from_config({}) == DayExperienceSettings(time(4, 0))
    assert DayExperienceSettings.from_config({"at": "05:30"}) == DayExperienceSettings(time(5, 30))
    assert DayExperienceSettings.from_config({"enabled": False, "at": "05:30"}) is None
    assert DayExperienceSettings.from_config({"at": "soon"}) is None


def test_the_schedule_is_due_once_per_date_after_the_time(tmp_path: Path) -> None:
    """Not before ``at``, due after it, and not again for a date it already tried."""
    schedule = DayExperienceSchedule(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=DayExperienceSettings(time(4, 0)),
        event_log_path=tmp_path / "events.db",
    )
    early, late = _noon(DAY).replace(hour=3), _noon(DAY).replace(hour=5)
    assert schedule.due(early) is None
    assert schedule.due(late) == DAY
    open_event_log(tmp_path / "events.db").close()
    assert schedule.write(DAY) == {}
    assert schedule.due(late) is None
    assert schedule.due(late + timedelta(days=1)) == DAY + timedelta(days=1)


def test_the_report_prints_the_newest_day_and_the_week_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Plain text from the stored rows; a date with no row is an error, nothing is written."""
    log, memory = tmp_path / "events.db", tmp_path / "memory.db"
    _log(log, [
        ("utterance.received", _noon(DAY), {"transcript": "hi", "turn_id": "T1"}, "mac"),
        ("route.oneshot_read", _noon(DAY, 1), {"turn_id": "T1", "tool_group": "web_search"}, "mac"),
        ("turn.failed", _noon(DAY, 2), {"turn_id": "T1", "exception_repr": "x"}, "mac"),
    ])
    run_day_experience(memory, log, DAY + timedelta(days=1))
    before = memory.read_bytes()
    assert experience_report.main(["--db", str(memory), "--week"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == DAY.isoformat()
    assert "uses: 1 (1 spoken, 0 typed) [mac 1 spoken 0 typed]" in out
    assert "right first time: 0/1 = 0% (signals counted: guard_repeat 0," in out
    assert "turn_failed 1" in out
    assert "most used web_search (1), most failed web_search (1 of 1 not right" in out
    assert experience_report.main(["--db", str(memory), "--date", DAY.isoformat()]) == 0
    capsys.readouterr()
    assert experience_report.main(["--db", str(memory), "--date", "2026-01-01"]) == 1
    assert experience_report.main(["--db", str(tmp_path / "absent.db")]) == 1
    assert memory.read_bytes() == before
