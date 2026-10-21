"""A watcher anchors a cursor at the log's tip and then reads only the rows after it."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from jarvis.state.event_log import fetch_events_after, latest_id, open_event_log
from tests.support.random_log import emit_row

if TYPE_CHECKING:
    from pathlib import Path


def test_an_empty_log_is_anchored_at_zero(tmp_path: Path) -> None:
    """With no rows the cursor starts at 0, so the first row ever written is read."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        assert latest_id(conn) == 0
        assert fetch_events_after(conn, after_id=0, event_types=("surface.response_open",)) == []


def test_the_anchor_is_the_last_row_whatever_its_type(tmp_path: Path) -> None:
    """The tip counts every row, so a watcher never replays what landed before it started."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        emit_row(conn, "surface.response_open", turn_id="T1")
        emit_row(conn, "surface.response_chunk", turn_id="T1")
        last = emit_row(conn, "surface.response_emitted", turn_id="T1")
        tip = latest_id(conn)
        assert tip == 3
        assert fetch_events_after(conn, after_id=tip, event_types=(last.type,)) == []


def test_rows_of_several_types_come_back_in_append_order_after_the_cursor(tmp_path: Path) -> None:
    """One cursor over many types keeps their interleaving; other types and older rows stay out."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        emit_row(conn, "surface.response_open", turn_id="T1")
        anchor = latest_id(conn)
        chunk = emit_row(conn, "surface.response_chunk", turn_id="T1")
        emit_row(conn, "surface.playback_started", turn_id="T1")
        emitted = emit_row(conn, "surface.response_emitted", turn_id="T1")
        got = fetch_events_after(
            conn,
            after_id=anchor,
            event_types=("surface.response_emitted", "surface.response_chunk"),
        )
        assert got == [(anchor + 1, chunk), (anchor + 3, emitted)]


def test_advancing_the_cursor_to_the_last_row_reads_nothing_twice(tmp_path: Path) -> None:
    """A watcher that moves its cursor to the last id it handled sees each row once."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        types = ("surface.response_open", "surface.response_chunk")
        emit_row(conn, "surface.response_open", turn_id="T1")
        emit_row(conn, "surface.response_chunk", turn_id="T1")
        first = fetch_events_after(conn, after_id=0, event_types=types)
        cursor = max(row_id for row_id, _event in first)
        assert len(first) == 2
        assert fetch_events_after(conn, after_id=cursor, event_types=types) == []
        late = emit_row(conn, "surface.response_chunk", turn_id="T1")
        assert fetch_events_after(conn, after_id=cursor, event_types=types) == [(cursor + 1, late)]
