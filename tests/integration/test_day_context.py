"""``session.context: day_summaries`` (ADR 0149): the history's layout and what stops.

Real memory.db and real ``render_context`` / ``CompactionSweep``; the days, records
and summaries are made up. Day summaries are stored with their ``## YYYY-MM-DD``
heading, as the nightly job writes them.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from jarvis.runtime.session_compaction import CompactionSweep
from jarvis.shared import lang
from jarvis.state.memory_db import (
    DAY_SUMMARIES_CONTEXT,
    MemorySettings,
    SessionSettings,
    append_day_summary,
    append_summary,
    open_memory_db,
    record_sent,
    render_context,
)
from tests.integration.test_rolling_summary import _fill, _summaries, _Summariser, _tick

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 3, 9, 0).astimezone()
HEADER = (
    "[Earlier days · summaries only · for exact words, numbers or details of these or "
    "older days use recall, search_records, read_records]"
)
BOUNDARY = (
    "[From 2026-10-01 00:00 on, every word said is below; before that only the summaries "
    "above and the core memory]"
)


def _at(day: int, hour: int, minute: int = 0) -> str:
    return datetime(2026, 10, day, hour, minute).astimezone().isoformat(timespec="seconds")


def _before(hour: int) -> str:
    return datetime(2026, 9, 30, hour).astimezone().isoformat(timespec="seconds")


def _insert(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    with closing(open_memory_db(path)) as conn, conn:
        conn.executemany("INSERT INTO records (id, ts, source, text) VALUES (?, ?, ?, ?)", rows)


def _summarise(path: Path, *days: str) -> None:
    for day in days:
        append_day_summary(
            path,
            day=day,
            summary=f"## {day}\n### Topics\n- topic of {day}",
            model="m",
            record_count=1,
            input_chars=1,
            output_chars=1,
        )


def _render(path: Path, **kwargs: object) -> tuple[dict[str, str], ...]:
    return render_context(path, exclude_id="live", now=NOW, **kwargs).history  # type: ignore[arg-type]


def _store(tmp_path: Path) -> Path:
    """Records of 9-30 to 10-02 and a live one; summaries for 9-27 to 9-30, so B is 10-01."""
    path = tmp_path / "memory.db"
    _insert(
        path,
        [
            ("a1", _before(20), "allen", "old question"),
            ("a2", _before(21), "jarvis", "old answer"),
            ("b1", _at(1, 8), "allen", "morning of the first"),
            ("b2", _at(1, 8, 1), "jarvis", "answer of the first"),
            ("b3", _at(1, 8, 2), "allen", "kept as sent"),
            ("c1", _at(2, 9), "allen", "second day"),
            ("c2", _at(2, 9, 1), "jarvis", "second answer"),
            ("live", _at(3, 9), "allen", "this turn"),
        ],
    )
    record_sent(path, "b3", "[State]\nkept as sent")
    _summarise(path, "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30")
    return path


def test_three_summaries_then_the_boundary_then_every_record_from_it(tmp_path: Path) -> None:
    """Three latest summaries oldest first, the boundary line, then every record from B on."""
    path = _store(tmp_path)
    append_summary(
        path,
        base_id=None,
        upto_record_id="a1",
        summary="ROLLING",
        model="m",
        input_chars=1,
        output_chars=1,
    )
    history = _render(path, context=DAY_SUMMARIES_CONTEXT, recent=2)
    summaries = "\n\n".join(
        f"## 2026-09-{day}\n### Topics\n- topic of 2026-09-{day}" for day in (28, 29, 30)
    )
    first_marker = lang.day_marker(date(2026, 10, 1))
    second_marker = lang.day_marker(date(2026, 10, 2))
    assert history[0] == {
        "role": "user",
        "content": f"{HEADER}\n\n{summaries}\n{BOUNDARY}\n{first_marker}\nmorning of the first",
    }
    assert history[1:] == (
        {"role": "assistant", "content": "answer of the first"},
        {"role": "user", "content": f"[State]\nkept as sent\n{second_marker}\nsecond day"},
        {"role": "assistant", "content": "second answer"},
    )
    text = str(history)
    assert "ROLLING" not in text
    assert "2026-09-27" not in text
    assert "old question" not in text
    assert "this turn" not in text


def test_history_since_still_floors_the_raw_records(tmp_path: Path) -> None:
    """A ``history_since`` after the boundary still cuts the raw records."""
    path = _store(tmp_path)
    history = _render(path, context=DAY_SUMMARIES_CONTEXT, since=_at(2, 0))
    text = str(history)
    assert "morning of the first" not in text
    assert "second day" in text
    assert BOUNDARY in text


def test_over_the_cap_the_oldest_hide_in_blocks_of_fifty_and_the_cut_holds(
    tmp_path: Path,
) -> None:
    """The cut is a multiple of 50, notes how many, and one more record does not move it."""
    path = tmp_path / "memory.db"
    _summarise(path, "2026-09-30")
    rows = [
        (f"r{i:03d}", _at(1, 8, i % 60) if i < 60 else _at(1, 9, i % 60), "allen", f"x{i:03d}")
        for i in range(120)
    ]
    _insert(path, rows)  # 120 records of 4 characters

    def history(cap: int) -> str:
        return str(_render(path, context=DAY_SUMMARIES_CONTEXT, raw_max_chars=cap))

    assert "Earlier conversation" not in history(480)
    cut = history(300)  # 480 > 300 -> hide 50 (280 left) in one block
    assert "[Earlier conversation · 50 records before 2026-10-01T08:50:00" in cut
    assert "use recall or search_records]" in cut
    assert "x049" not in cut
    assert "x050" in cut
    assert "x119" in cut
    two = history(200)  # 280 > 200 -> a second block
    assert "[Earlier conversation · 100 records before" in two
    assert "x099" not in two
    assert "x100" in two
    # One more record does not move the cut: the head stays the same for the cache.
    _insert(path, [("r999", _at(1, 10), "allen", "x999")])
    assert "[Earlier conversation · 50 records before" in history(300)
    assert "x050" in history(300)


def test_without_a_day_summary_it_is_the_rolling_layout(tmp_path: Path) -> None:
    """No day summary stored: the turn renders exactly as rolling mode does."""
    path = tmp_path / "memory.db"
    _insert(path, [("a1", _at(1, 8), "allen", "hello"), ("a2", _at(1, 8, 1), "jarvis", "hi")])
    append_summary(
        path,
        base_id=None,
        upto_record_id="a1",
        summary="ROLLING",
        model="m",
        input_chars=1,
        output_chars=1,
    )
    day = _render(path, context=DAY_SUMMARIES_CONTEXT)
    assert day == _render(path)
    assert "ROLLING" in str(day)
    assert "Earlier days" not in str(day)


def test_rolling_is_unchanged_by_the_new_keys(tmp_path: Path) -> None:
    """Absent or ``rolling`` is today's layout; the settings parse as documented."""
    path = _store(tmp_path)
    rolling = _render(path)
    assert rolling == _render(path, context="rolling", raw_max_chars=1)
    assert "Earlier days" not in str(rolling)
    assert "old question" in str(rolling)
    assert SessionSettings.from_config({}).context == "rolling"
    assert SessionSettings.from_config({"context": "nonsense"}).context == "rolling"
    on = SessionSettings.from_config({"context": "day_summaries", "context_raw_max_chars": 99})
    assert (on.context, on.context_raw_max_chars) == (DAY_SUMMARIES_CONTEXT, 99)
    assert SessionSettings().context_raw_max_chars == 40000


def _sweep(tmp_path: Path, summariser: _Summariser, **settings: object) -> CompactionSweep:
    from jarvis.state.event_log import open_event_log  # noqa: PLC0415

    log = tmp_path / "events.db"
    open_event_log(log).close()
    sweep = CompactionSweep(
        memory=MemorySettings(db_path=tmp_path / "memory.db", audio_dir=tmp_path / "audio"),
        settings=SessionSettings(compact_prompt="summarise", recent_records=20, **settings),  # type: ignore[arg-type]
        llm_config={},
        event_log_path=log,
        pricing_table={},
        context_length=lambda: 1_050_000,
        live_open=lambda: False,
    )
    sweep._client = summariser  # type: ignore[assignment]  # noqa: SLF001 — the provider seam
    return sweep


def test_the_compaction_sweep_does_nothing_in_this_mode(tmp_path: Path) -> None:
    """The window fold runs in rolling mode (control) and never in day_summaries mode."""
    _fill(tmp_path / "memory.db", 0, 149)
    control = _Summariser()
    _tick(_sweep(tmp_path, control))
    assert control.requests  # the window folds what it hides in rolling mode

    other = tmp_path / "other"
    other.mkdir()
    _fill(other / "memory.db", 0, 149)
    quiet = _Summariser()
    _tick(_sweep(other, quiet, context=DAY_SUMMARIES_CONTEXT))
    assert quiet.requests == []
    assert _summaries(other / "memory.db") == []


def test_a_gap_since_the_last_record_is_still_noted_with_no_raw_record(tmp_path: Path) -> None:
    """With no raw record the time line still counts from the last record."""
    path = tmp_path / "memory.db"
    _insert(path, [("a1", _at(1, 8), "allen", "hello")])
    _summarise(path, "2026-10-01")
    context = render_context(
        path,
        exclude_id="live",
        now=NOW + timedelta(days=1),
        context=DAY_SUMMARIES_CONTEXT,
    )
    assert "since the last exchange" in context.now
