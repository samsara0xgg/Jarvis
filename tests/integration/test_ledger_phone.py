"""ADR 0201 — the phone in the ledger: his sleep, steps, workouts and places, from the event log.

Acceptance checks against the real code and a real event log, each a table of phone rows to the
exact lines the ledger prints:

- a night's sleep across midnight belongs to the morning he woke, with its bed and wake times;
- steps and workouts are summed by local day (a span over midnight is split, a total reported twice
  for one span counts once), places are the named stays only, longest first, cut at the midnights;
- a day without phone rows prints nothing for the phone;
- the same lines in the per-turn "Today so far" block, the open stay measured to now;
- the numbers equal the day line's own fold of the same rows (ADR 0199);
- a late batch for a finished day rebuilds the cached standing text, a batch of today does not.

Times are in the ledger's zone (UTC-7), Thursday 2026-10-08 15:00 being now.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from jarvis.runtime.ledger import LedgerContext, LedgerSettings
from jarvis.state import phone_day
from jarvis.state.day_line import DaySources, fold_day
from jarvis.state.event_log import open_event_log
from jarvis.state.ledger import (
    LedgerSources,
    day_numbers_text,
    hm,
    phone_stamp,
    standing_text,
    today_text,
)
from tests.integration.test_day_line import Row, _load, health, visit
from tests.integration.test_ledger import MIDNIGHT, NOW, ZONE, _world

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

ORIGIN = int(datetime(2026, 10, 4, tzinfo=ZONE).timestamp() * 1000)  # Sunday 00:00
STEPS, WORKOUT = "steps", "workout_min"


def at(day: int, hour: int, minute: int = 0) -> float:
    """Minutes from Sunday 10-04 00:00 to ``day`` of October at ``hour:minute``."""
    return (day - 4) * 1440 + hour * 60 + minute


def phone_rows() -> list[Row]:
    """Mon 10-05 is full, Tue 10-06 has nothing, Wed 10-07 crosses midnight both ways, Thu is today.

    The compact stretch (more than seven days back) has one day, Wed 09-30.
    """
    return [
        # Sunday night to Monday morning, in three stages with awake gaps: 390 minutes asleep.
        health("sl1", at(4, 23, 30), at(5, 1), 90),
        health("sl2", at(5, 1, 10), at(5, 3), 110),
        health("sl3", at(5, 3, 20), at(5, 6, 30), 190),
        # Monday steps; the first window is reported twice and the later total stands.
        health("st1", at(5, 8), at(5, 9), 1200, STEPS),
        health("st1b", at(5, 8), at(5, 9), 1250, STEPS),
        health("st2", at(5, 12), at(5, 13), 3000.5, STEPS),
        health("st3", at(5, 18), at(5, 19), 2219.5, STEPS),
        health("wo1", at(5, 17), at(5, 17, 45), 45, WORKOUT),
        # Monday places: Home before dawn (arrived Sunday), the library, a short coffee, an
        # unnamed stay, Home again until 23:00. Arrivals are reported first, then with departures.
        visit("v0", at(4, 22), at(5, 8, 10), "Home"),
        visit("v1a", at(5, 8, 30), None, "Library"),
        visit("v1", at(5, 8, 30), at(5, 12), "Library"),
        visit("v2", at(5, 12, 20), at(5, 12, 30), "Cafe"),
        visit("v3", at(5, 13), at(5, 14)),
        visit("v4", at(5, 14, 30), at(5, 23), "Home"),
        # Tuesday night to Wednesday morning: the sleep belongs to Wednesday.
        health("sl4", at(6, 23, 45), at(7, 3), 200),
        health("sl5", at(7, 3, 10), at(7, 7, 15), 230),
        # Wednesday steps (the last window runs past midnight: half is Wednesday's) and places.
        health("st4", at(7, 9), at(7, 10), 6000, STEPS),
        health("st5", at(7, 23, 30), at(8, 0, 30), 1000, STEPS),
        visit("v5", at(7, 18), at(7, 19), "Gym"),
        visit("v6", at(7, 19, 30), None, "Home"),  # still there when the day ends
        # Wednesday night to Thursday morning, and Thursday so far.
        health("sl6", at(7, 23, 50), at(8, 7, 20), 440),
        health("st6", at(8, 7, 30), at(8, 8, 30), 800, STEPS),
        health("st7", at(8, 10), at(8, 11), 1500, STEPS),
        # The compact stretch (day 0 of October is Wednesday 09-30): woke at 06:00, 420 minutes.
        health("sl7", at(-1, 23), at(0, 6), 420),
        health("st8", at(0, 9), at(0, 10), 9000, STEPS),
    ]


def sources(tmp_path: Path) -> LedgerSources:
    """The ledger world of ``test_ledger`` with the phone's rows in its Event Log."""
    src = _world(tmp_path)
    with closing(open_event_log(src.event_log)) as conn:
        _load(conn, phone_rows(), ORIGIN)
    return src


def day_block(text: str, heading: str) -> list[str]:
    """The indented lines under a day's heading in the "in full" list."""
    lines = text.splitlines()
    first = lines.index(heading) + 1
    found: list[str] = []
    for line in lines[first:]:
        if not line.startswith("  "):
            break
        found.append(line)
    return found


def test_recent_days_carry_the_phone_lines_exactly(tmp_path: Path) -> None:
    """Sleep by wake-up, steps, workout and places per day; the empty day prints no phone line."""
    text = standing_text(sources(tmp_path), MIDNIGHT)
    monday = day_block(text, "2026-10-05 Mon")
    assert (
        "  Health (phone): sleep 6h30m (23:30 the evening before to 06:30), steps 6470, "
        "workout 0h45m" in monday
    )
    assert "  Places (phone): Home 16h40m, Library 3h30m, Cafe 0h10m" in monday
    # Tuesday: no phone row falls on it, so the phone says nothing, and nothing says he rested.
    tuesday = day_block(text, "2026-10-06 Tue")
    assert tuesday
    assert not [line for line in tuesday if "(phone)" in line or "steps" in line]
    # Wednesday: the sleep from Tuesday 23:45 to 07:15 is his Wednesday; half of the window
    # that spans midnight is his; the open stay is cut at the midnight.
    wednesday = day_block(text, "2026-10-07 Wed")
    assert "  Health (phone): sleep 7h10m (23:45 the evening before to 07:15), steps 6500" in (
        wednesday
    )
    assert "  Places (phone): Home 4h30m, Gym 1h00m" in wednesday
    # The compact stretch: one line per day, the phone's part only for what was reported.
    lines = [entry.strip() for entry in text.splitlines()]
    reported = next(x for x in lines if x.startswith("09-30 Wed:"))
    assert reported.endswith("; sleep 7h00m; steps 9000")
    quiet = next(x for x in lines if x.startswith("09-29 Tue:"))
    assert "sleep" not in quiet
    assert "steps" not in quiet
    assert "workout" not in quiet


def test_today_so_far_carries_the_phone_up_to_now(tmp_path: Path) -> None:
    """Last night's sleep is today's; steps split at midnight; the open stay runs to now."""
    today = today_text(sources(tmp_path), NOW)
    assert (
        "  Health (phone): sleep 7h20m (23:50 the evening before to 07:20), steps 2800" in today
    )
    # Home since Wednesday 19:30 is open: today it is 00:00 to 15:00.
    assert "  Places (phone): Home 15h00m" in today
    assert "workout" not in today


def test_a_day_with_no_phone_rows_prints_no_phone_line(tmp_path: Path) -> None:
    """The ledger of the world without a phone is the ledger it was."""
    src = _world(tmp_path)
    assert "(phone)" not in standing_text(src, MIDNIGHT)
    assert "(phone)" not in today_text(src, NOW)


def test_the_ledger_and_the_day_line_agree_on_the_same_rows(tmp_path: Path) -> None:
    """Oracle: the day line's own items for Monday, in the style of ``day_numbers_text``'s check."""
    src = sources(tmp_path)
    monday = datetime(2026, 10, 5, tzinfo=ZONE)
    start = int(monday.timestamp() * 1000)
    end = int((monday + timedelta(days=1)).timestamp() * 1000)
    with closing(open_event_log(src.event_log)) as conn:
        items = fold_day(conn, start, end, end, DaySources(ZONE)).items
    told = day_numbers_text(src, monday.date(), monday + timedelta(days=1))
    # Sleep: the block that ended on Monday, in minutes, is the hours she is told.
    blocks = [i for i in items if i["kind"] == "sleep" and start <= i["end_ms"] < end]
    assert [hm(b["minutes"] * 60) for b in blocks] == ["6h30m"]
    assert f"sleep {hm(blocks[0]['minutes'] * 60)} (" in told
    # Places: the named stays cut to Monday and summed by name, as she is told them.
    held: dict[str, float] = {}
    for item in items:
        if item["kind"] == "stay" and item.get("place"):
            seconds = (min(item["end_ms"], end) - max(item["start_ms"], start)) / 1000
            held[item["place"]] = held.get(item["place"], 0) + seconds
    ranked = sorted(held.items(), key=lambda pair: -pair[1])
    assert "Places (phone): " + ", ".join(f"{k} {hm(v)}" for k, v in ranked) in told


def test_a_late_batch_for_a_finished_day_rebuilds_the_cache_and_a_batch_of_today_does_not(
    tmp_path: Path,
) -> None:
    """Standing text is keyed on the finished days' rows; today's live numbers are per turn."""
    src = sources(tmp_path)
    context = LedgerContext(src, LedgerSettings())
    standing, per_turn = context(NOW, None)
    assert "steps 6470" in standing
    assert "steps 2800" in per_turn
    stamp = phone_stamp(src, MIDNIGHT)
    # A batch of today: the per-turn numbers move, the cached text is the same object.
    with closing(open_event_log(src.event_log)) as conn:
        _load(conn, [health("st9", at(8, 12), at(8, 13), 700, STEPS)], ORIGIN)
    again, live = context(NOW, None)
    assert again is standing
    assert phone_stamp(src, MIDNIGHT) == stamp
    assert "steps 3500" in live
    # A late batch for Monday's evening arrives: the text is rebuilt with it.
    with closing(open_event_log(src.event_log)) as conn:
        _load(conn, [health("st10", at(5, 20), at(5, 21), 530, STEPS)], ORIGIN)
    assert phone_stamp(src, MIDNIGHT) != stamp
    rebuilt = context(NOW, None)[0]
    assert rebuilt is not standing
    assert "steps 7000" in rebuilt


def test_unreadable_phone_rows_cost_the_phone_lines_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone read that raises is logged; the rest of the text is whole and has no phone line."""
    src = sources(tmp_path)

    def broken(*_args: object, **_kwargs: object) -> None:
        msg = "the log fell over"
        raise RuntimeError(msg)

    monkeypatch.setattr(phone_day, "read_rows", broken)
    text = standing_text(src, MIDNIGHT)
    assert "Computer: started 09:00, stopped 10:30, active 1h30m" in text
    assert "(phone)" not in text
    assert "(phone)" not in today_text(src, NOW)
    # A log that cannot be opened has no stamp to go stale on.
    gone = LedgerSources(src.memory_db, tmp_path / "gone.db", None, ZONE)
    assert phone_stamp(gone, MIDNIGHT) == (0, 0)
