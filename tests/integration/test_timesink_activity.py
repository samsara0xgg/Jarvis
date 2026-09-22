"""Read-only TimeSink integration through Jarvis's real tool dispatcher.

query_activity answers as one compact table (ADR 0029): a header with the app dictionary
and whole-window totals, then ``[ref, start, end, app, text]`` rows sized to a character
budget. These checks assert on that persisted tool output.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import _register_workers, _timesink_db_path
from jarvis.state import daily_activity
from jarvis.state.daily_contract import ACTIVITY_PAGE_BUDGET
from jarvis.state.event_log import emit_event
from tests.integration.test_daily_tools import DailyHarness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

QUERY = {"from": "2026-09-19T09:00:00Z", "to": "2026-09-19T10:00:00Z", "sources": ["app"]}
SCREEN_QUERY = {**QUERY, "sources": ["app", "screen"]}
REV = 8  # hex digits of the row revision inside a short ref


@pytest.fixture
def source(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A live WAL writer using the real TimeSink schema and GRDB timestamp encoding."""
    conn = sqlite3.connect(tmp_path / "timesink.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE span (id INTEGER PRIMARY KEY AUTOINCREMENT, start DATETIME NOT NULL, "
        "end DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "title TEXT, url TEXT, domain TEXT)"
    )
    conn.execute(
        "CREATE TABLE stateEvent (id INTEGER PRIMARY KEY AUTOINCREMENT, at DATETIME NOT NULL, "
        "kind TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE capture (id INTEGER PRIMARY KEY AUTOINCREMENT, at DATETIME NOT NULL, "
        "lastSeenAt DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "windowID INTEGER NOT NULL, title TEXT, spanID INTEGER, text TEXT NOT NULL, "
        "imagePath TEXT)"
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def one_row_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page budget below one row: a page still carries one row, so paging is cheap to test."""
    monkeypatch.setattr(daily_activity, "ACTIVITY_PAGE_BUDGET", 1)


@pytest.fixture
def connected(tmp_path: Path, source: sqlite3.Connection) -> Iterator[DailyHarness]:
    """Wire TimeSink into the default registry with actual on-disk stores."""
    del source
    harness = DailyHarness(tmp_path, timesink_path=tmp_path / "timesink.sqlite")
    yield harness
    harness.fx.close()


def add_span(  # noqa: PLR0913 — the columns TimeSink writes.
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    title: str = "Jarvis notes",
    bundle: str = "com.google.Chrome",
    name: str = "Chrome",
) -> int:
    """Write source data exactly as the upstream producer would."""
    cursor = conn.execute(
        "INSERT INTO span(start,end,appBundleID,appName,title,url,domain) VALUES(?,?,?,?,?,?,?)",
        (start, end, bundle, name, title, "https://example.test/docs", "example.test"),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_capture(
    conn: sqlite3.Connection, at: str, last_seen: str, *, text: str, image: str | None = "d/1.jpg"
) -> int:
    """Write a screen capture exactly as TimeSink's ScreenCollector does."""
    cursor = conn.execute(
        "INSERT INTO capture(at,lastSeenAt,appBundleID,appName,windowID,title,spanID,text,"
        "imagePath) VALUES(?,?,?,?,?,?,?,?,?)",
        (at, last_seen, "com.google.Chrome", "Chrome", 13117, "cc | rules", None, text, image),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def add_event(conn: sqlite3.Connection, at: str, kind: str) -> None:
    """Write a tracker state reason exactly as TimeSink's ObservationStore does."""
    conn.execute("INSERT INTO stateEvent(at,kind) VALUES(?,?)", (at, kind))
    conn.commit()


def read_all(harness: DailyHarness, activity_id: str) -> dict[str, Any]:
    """Follow read_activity's cursor to the end; content is the whole original."""
    args: dict[str, Any] = {"activity_id": activity_id}
    content = ""
    while True:
        page = harness.call("read_activity", args)
        assert "code" not in page, page
        assert page["offset"] == len(content)
        content += page["content"]
        if page["next_cursor"] is None:
            return {**page, "content": content}
        args["cursor"] = page["next_cursor"]


def collect(harness: DailyHarness, args: dict[str, Any]) -> tuple[list[Any], list[Any], int]:
    """Follow next_cursor to the end, returning every row, every state event and the page count."""
    rows: list[Any] = []
    events: list[Any] = []
    pages = 0
    cursor = None
    while True:
        page = harness.call("query_activity", {**args, **({"cursor": cursor} if cursor else {})})
        assert "code" not in page, page
        pages += 1
        rows += page["rows"]
        events += page["state_events"]
        cursor = page["next_cursor"]
        if cursor is None:
            return rows, events, pages


def test_compact_row_and_readback_round_trip(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """Include the clipped tail of earlier spans, exclude exact boundaries and zero spans."""
    add_span(source, "2026-09-19 08:50:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 08:00:00.000", "2026-09-19 09:00:00.000")
    add_span(source, "2026-09-19 10:00:00.000", "2026-09-19 10:10:00.000")
    add_span(source, "2026-09-19 09:15:00.000", "2026-09-19 09:15:00.000")
    before = source.execute("SELECT * FROM span").fetchall()
    result = connected.call("query_activity", QUERY)
    assert result["date"] == "2026-09-19"
    assert result["utc_offset"] == "+00:00"
    assert result["columns"] == ["ref", "start", "end", "app", "text"]
    assert result["count"] == result["total"] == 1
    ref, start, end, app, text = result["rows"][0]
    assert ref.startswith("s1:")
    assert len(ref) == len("s1:") + REV
    assert (start, end, app) == ("08:50:00", "09:10:00", "A")
    assert text == "Jarvis notes (example.test)"
    assert result["apps"] == {"A": ["Chrome", "com.google.Chrome"]}
    # Totals clip to the window: only the ten minutes inside [from,to) count.
    assert result["totals"]["A"] == {
        "foreground_s": 600,
        "spans": 1,
        "first": "09:00:00",
        "last": "09:10:00",
    }
    assert result["coverage"]["app"]["status"] == "partial"
    assert result["coverage"]["app"]["gap_reason"] == "unknown"
    assert result["notes"], "the first page explains the table once"
    detail = connected.call("read_activity", {"activity_id": ref})
    assert json.loads(detail["content"])["url"] == "https://example.test/docs"
    assert detail["source_refs"] == [
        f"timesink:{result['store']}:1:{detail['source_refs'][0].rsplit(':', 1)[1]}"
    ]
    full = detail["source_refs"][0]
    assert len(full.rsplit(":", 1)[1]) == 32
    assert full.startswith(f"timesink:{result['store']}:{ref[1:]}")
    # The full reference restored from the row ref reads the same row.
    restored = connected.call(
        "read_activity", {"activity_id": f"timesink:{result['store']}:{ref[1:]}"}
    )
    assert restored["source_refs"] == detail["source_refs"]
    assert source.execute("SELECT * FROM span").fetchall() == before
    assert connected.call("query_activity", {**QUERY, "project": "/repos/jarvis"})["rows"] == []


@pytest.mark.usefixtures("one_row_pages")
def test_paging_excludes_new_rows_and_detects_corrections(
    connected: DailyHarness,
    source: sqlite3.Connection,
) -> None:
    """An append watermark alone cannot freeze a TimeSink row's mutable end time."""
    first_id = add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 09:20:00.000", "2026-09-19 09:30:00.000")
    page = connected.call("query_activity", QUERY)
    assert page["count"] == 1
    assert page["total"] == 2
    assert page["next_cursor"] is not None
    assert "notes" in page
    add_span(source, "2026-09-19 09:15:00.000", "2026-09-19 09:16:00.000")
    second = connected.call("query_activity", {**QUERY, "cursor": page["next_cursor"]})
    assert second["next_cursor"] is None
    assert second["offset"] == 1
    assert second["rows"][0][1] == "09:20:00"
    assert "notes" not in second, "continuation pages repeat only the data"
    source.execute("UPDATE span SET end='2026-09-19 09:05:00.000' WHERE id=?", (first_id,))
    source.commit()
    stale = connected.call("query_activity", {**QUERY, "cursor": page["next_cursor"]})
    assert stale["code"] == "invalid_cursor"
    assert "changed" in stale["error"]
    assert (
        connected.call("read_activity", {"activity_id": page["rows"][0][0]})["code"]
        == "source_changed"
    )
    fresh = connected.call("query_activity", QUERY)
    assert fresh["total"] == 3
    assert fresh["rows"][0][2] == "09:05:00"
    assert fresh["totals"]["A"]["foreground_s"] == 300 + 60 + 600
    assert fresh["totals"]["A"]["spans"] == 3


@pytest.mark.usefixtures("one_row_pages")
def test_mixed_git_and_app_order_and_missing_source_isolation(
    connected: DailyHarness,
    source: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Each source uses its declared clock; an unavailable app source cannot hide Git."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    emit_event(
        connected.fx.conn,
        type="project.commit_seen",
        ts_epoch_ms=1789809000000,
        payload={
            "repo_path": "/repos/jarvis",
            "commit_sha": "abc",
            "subject": "Fix",
            "committed_at_ms": 1000,
            "actor": "observer",
            "truncated": False,
            "skipped_count": 0,
        },
    )
    args = {**QUERY, "sources": ["app", "git", "screen"]}
    page = connected.call("query_activity", args)
    assert page["rows"][0][0].startswith("s1:")
    assert page["coverage"]["screen"]["status"] == "partial"
    following = connected.call("query_activity", {**args, "cursor": page["next_cursor"]})
    ref, observed, end, app, text = following["rows"][0]
    assert ref.startswith("g")
    assert len(ref) == 33
    assert (observed, end, app) == ("09:10:00", "", "git")
    # The commit time keeps its year when it is not the page's year.
    assert text == "/repos/jarvis: Fix (committed 1970-01-01 00:00:01)"
    detail = connected.call("read_activity", {"activity_id": ref})
    assert detail["source_refs"] == [f"event:{ref[1:]}"]
    assert json.loads(detail["content"])["commit_sha"] == "abc"
    # Stop the source first, so its WAL is checkpointed before moving the file.
    source.close()
    (tmp_path / "timesink.sqlite").rename(tmp_path / "offline.sqlite")
    unavailable = connected.call("query_activity", args)
    assert unavailable["coverage"]["app"]["status"] == "unavailable"
    assert unavailable["store"] is None
    assert unavailable["rows"][0][3] == "git"
    assert not (tmp_path / "timesink.sqlite").exists()


def test_exact_long_detail_and_revision_refs(
    connected: DailyHarness,
    source: sqlite3.Connection,
) -> None:
    """Bounded row text keeps the exact original readable and its provenance saveable."""
    title = 'notes\n"\\世界' * 3000
    identity = add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000", title=title)
    result = connected.call("query_activity", QUERY)
    row = result["rows"][0]
    assert len(row[4]) == 120
    assert title.startswith(row[4])
    detail = read_all(connected, row[0])
    assert json.loads(detail["content"])["title"] == title
    knowledge = {
        "statement": "Read documentation",
        "kind": "fact",
        "basis": "observation",
        "source_refs": detail["source_refs"],
        "request_id": "knowledge",
    }
    receipt = connected.call("save_knowledge", knowledge)
    assert receipt["version"] == 1
    # A saved reference may also be restored from the row: store + id + 8-hex revision.
    short_full = f"timesink:{result['store']}:{row[0][1:]}"
    assert (
        connected.call(
            "save_knowledge", {**knowledge, "source_refs": [short_full], "request_id": "short"}
        )["version"]
        == 1
    )
    todo = connected.call(
        "create_todo",
        {"title": "Follow up", "source_refs": detail["source_refs"], "request_id": "todo"},
    )
    briefing = connected.call(
        "save_briefing",
        {
            "local_date": "2026-09-19",
            "timezone": "America/Vancouver",
            "content": "Read docs",
            "source_refs": detail["source_refs"],
            "coverage": {"app": "partial"},
            "request_id": "brief",
        },
    )
    assert briefing["version"] == 1
    source.execute("UPDATE span SET title='Corrected' WHERE id=?", (identity,))
    source.commit()
    assert connected.call("save_knowledge", knowledge) == receipt
    assert (
        connected.call("save_knowledge", {**knowledge, "request_id": "new"})["code"]
        == "source_changed"
    )
    assert connected.call("read_activity", {"activity_id": short_full})["code"] == "source_changed"
    assert (
        connected.call(
            "update_todo",
            {
                "todo_id": todo["todo_id"],
                "expected_version": 1,
                "patch": {"status": "done"},
                "request_id": "done",
            },
        )["version"]
        == 2
    )


@pytest.mark.usefixtures("one_row_pages")
def test_database_identity_pins_full_refs_and_cursors(
    connected: DailyHarness,
    source: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Replacing a DB with identical rows must not silently reuse old evidence IDs."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 09:20:00.000", "2026-09-19 09:30:00.000")
    first = connected.call("query_activity", QUERY)
    full_ref = connected.call("read_activity", {"activity_id": first["rows"][0][0]})["source_refs"][
        0
    ]
    source.close()
    path = tmp_path / "timesink.sqlite"
    path.rename(tmp_path / "old.sqlite")
    with (
        closing(sqlite3.connect(tmp_path / "old.sqlite")) as old,
        closing(sqlite3.connect(path)) as new,
    ):
        old.backup(new)
    assert connected.call("read_activity", {"activity_id": full_ref})["code"] == "source_changed"
    assert (
        connected.call("query_activity", {**QUERY, "cursor": first["next_cursor"]})["code"]
        == "invalid_cursor"
    )
    # A short row ref names the configured store: it reads, and reports the new identity.
    reread = connected.call("read_activity", {"activity_id": first["rows"][0][0]})
    assert reread["source_refs"][0] != full_ref
    assert (
        reread["source_refs"][0].split(":")[1] == connected.call("query_activity", QUERY)["store"]
    )


def test_dst_window_and_partial_millisecond_boundary(
    connected: DailyHarness,
    source: sqlite3.Connection,
) -> None:
    """UTC GRDB dates respect explicit offsets, repeated local hours and exclusive ends."""
    add_span(source, "2026-11-01 08:00:00.000", "2026-11-01 10:00:00.000")
    result = connected.call(
        "query_activity",
        {**QUERY, "from": "2026-11-01T01:30:00-07:00", "to": "2026-11-01T01:30:00-08:00"},
    )
    assert result["totals"]["A"]["foreground_s"] == 3600
    assert result["utc_offset"] == "-07:00"
    assert result["rows"][0][1:3] == ["01:00:00", "03:00:00"]
    add_span(source, "2026-09-19 09:00:00.001", "2026-09-19 09:00:01.000")
    assert (
        connected.call("query_activity", {**QUERY, "to": "2026-09-19T09:00:00.001Z"})["total"] == 0
    )
    assert (
        connected.call("query_activity", {**QUERY, "to": "2026-09-19T09:00:00.001500Z"})["total"]
        == 1
    )


def test_missing_or_incompatible_database_is_explicit(tmp_path: Path) -> None:
    """Neither a nonexistent path nor another app's schema gets created or migrated."""
    path = tmp_path / "timesink.sqlite"
    h = DailyHarness(tmp_path, timesink_path=path)
    try:
        assert h.call("query_activity", QUERY)["coverage"]["app"]["status"] == "unavailable"
        assert not path.exists()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")
        assert h.call("query_activity", QUERY)["coverage"]["app"]["status"] == "unavailable"
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
                ("unrelated",)
            ]
    finally:
        h.fx.close()


def test_configuration_is_explicit_and_validated(tmp_path: Path) -> None:
    """Hand-built runtimes do not accidentally read the developer's activity history."""
    assert _timesink_db_path({}) is None
    assert _timesink_db_path({"observer": {"timesink": {"enabled": False}}}) is None
    path = tmp_path / "span.sqlite"
    assert (
        _timesink_db_path({"observer": {"timesink": {"enabled": True, "db_path": str(path)}}})
        == path
    )
    with pytest.raises(ValueError, match="db_path"):
        _timesink_db_path({"observer": {"timesink": {"enabled": True}}})


def test_codex_workers_coexist_with_local_activity_tools(
    tmp_path: Path, source: sqlite3.Connection
) -> None:
    """The runtime registers Codex workers alongside daily tools without starting a worker."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    h = DailyHarness(tmp_path, timesink_path=tmp_path / "timesink.sqlite")
    workers = _register_workers(h.fx.registry, h.fx.paths)
    try:
        names = [tool.name for tool in h.fx.registry.get_definitions()]
        assert len(names) == len(set(names))
        assert {
            "spawn_worker",
            "wait_worker",
            "send_input",
            "close_worker",
            "query_activity",
            "read_activity",
            "save_briefing",
        } <= set(names)
        result = h.call("query_activity", QUERY)
        assert result["total"] == 1
        assert h.call("read_activity", {"activity_id": result["rows"][0][0]})["complete"]
    finally:
        workers.stop()
        h.fx.close()


def test_screen_captures_state_events_and_full_text(
    connected: DailyHarness, source: sqlite3.Connection, tmp_path: Path
) -> None:
    """A screen row is one content stretch; its full OCR text and the gap reasons are readable."""
    long_text = "OCR line " * 2000
    (tmp_path / "captures" / "d").mkdir(parents=True)
    (tmp_path / "captures" / "d" / "1.jpg").write_bytes(b"jpeg")
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    capture_id = add_capture(
        source, "2026-09-19 08:59:00.000", "2026-09-19 09:04:00.000", text=long_text
    )
    add_capture(
        source, "2026-09-19 09:30:00.000", "2026-09-19 09:31:00.000", text="short", image=None
    )
    add_capture(source, "2026-09-19 10:00:00.000", "2026-09-19 10:05:00.000", text="later")
    add_event(source, "2026-09-19 09:12:00.000", "idle")
    add_event(source, "2026-09-19 09:40:00.000", "lock")

    result = connected.call("query_activity", SCREEN_QUERY)
    assert [row[0][0] for row in result["rows"]] == ["c", "s", "c"]
    first = result["rows"][0]
    assert first[1:4] == ["08:59:00", "09:04:00", "A"]
    assert first[4].startswith("cc | rules — OCR line OCR line")
    assert len(first[4]) == 200
    assert result["rows"][2][4] == "cc | rules — short"
    assert result["totals"]["A"] == {
        "foreground_s": 600,
        "spans": 1,
        "screen_rows": 2,
        "first": "09:00:00",
        "last": "09:10:00",
    }
    assert result["coverage"]["screen"]["status"] == "partial"
    assert result["coverage"]["screen"]["observations_in_window"] == 2
    assert result["state_events"] == [["09:12:00", "idle"], ["09:40:00", "lock"]]
    assert result["state_events_total"] == 2
    assert result["state_at_start"] == []
    assert result["state_coverage"]["status"] == "partial"
    assert result["state_coverage"]["events_in_window"] == 2

    detail = read_all(connected, first[0])
    assert detail["content_format"] == "text"
    assert detail["content"] == long_text
    assert detail["image_path"].endswith("/captures/d/1.jpg")
    assert detail["image_available"] is True
    assert detail["ended_at_basis"] == "last_seen"
    assert detail["source_refs"][0].startswith(f"timesink-capture:{result['store']}:{first[0][1:]}")
    assert (
        connected.call("read_activity", {"activity_id": result["rows"][2][0]})["image_available"]
        is False
    )

    knowledge = connected.call(
        "save_knowledge",
        {
            "statement": "Was editing capture rules",
            "kind": "fact",
            "basis": "observation",
            "source_refs": detail["source_refs"],
            "request_id": "k1",
        },
    )
    assert knowledge["version"] == 1
    source.execute(
        "UPDATE capture SET lastSeenAt='2026-09-19 09:06:00.000' WHERE id=?", (capture_id,)
    )
    source.commit()
    assert connected.call("read_activity", {"activity_id": first[0]})["code"] == "source_changed"
    assert connected.call("query_activity", {**SCREEN_QUERY, "project": "/repos/x"})["rows"] == []


def test_screen_source_on_a_store_without_capture_tables(tmp_path: Path) -> None:
    """A TimeSink build that predates screen capture reports screen=unavailable, app intact."""
    conn = sqlite3.connect(tmp_path / "old.sqlite")
    conn.execute(
        "CREATE TABLE span (id INTEGER PRIMARY KEY AUTOINCREMENT, start DATETIME NOT NULL, "
        "end DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "title TEXT, url TEXT, domain TEXT)"
    )
    conn.commit()
    add_span(conn, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    conn.close()
    h = DailyHarness(tmp_path, timesink_path=tmp_path / "old.sqlite")
    try:
        result = h.call("query_activity", SCREEN_QUERY)
        assert result["total"] == 1
        assert result["coverage"]["app"]["status"] == "partial"
        assert result["coverage"]["screen"]["status"] == "unavailable"
        assert result["state_events"] == []
        assert result["state_coverage"]["status"] == "unavailable"
    finally:
        h.fx.close()


def test_state_events_page_with_rows_and_are_never_dropped(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """2400 events plus long captures exceed one page budget; paging must deliver all of them."""
    for i in range(2400):
        add_event(
            source,
            f"2026-09-19 09:{i // 60:02d}:{i % 60:02d}.000",
            ("idle", "active")[i % 2],
        )
    for i in range(6):
        add_capture(
            source,
            f"2026-09-19 09:{i * 5:02d}:00.000",
            f"2026-09-19 09:{i * 5 + 1:02d}:00.000",
            text="x" * 1500,
        )
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    first = connected.call("query_activity", SCREEN_QUERY)
    assert first["next_cursor"] is not None
    assert first["state_coverage"]["events_in_window"] == 2400
    assert first["state_events_total"] == 2400
    assert first["count"] == first["total"] == 7, "rows fit; the events are what page"
    add_event(source, "2026-09-19 09:30:00.500", "lock")  # after the first page: not in this paging
    rows, events, pages = collect(connected, {**SCREEN_QUERY, "cursor": first["next_cursor"]})
    rows = first["rows"] + rows
    events = first["state_events"] + events
    assert pages >= 1
    assert len(rows) == 7
    assert len(events) == 2400
    assert events == sorted(events)
    assert [event[1] for event in events[:2]] == ["idle", "active"]
    fresh = connected.call("query_activity", SCREEN_QUERY)
    assert fresh["state_coverage"]["events_in_window"] == 2401
    narrow = connected.call(
        "query_activity",
        {**SCREEN_QUERY, "from": "2026-09-19T09:10:00Z", "to": "2026-09-19T09:11:00Z"},
    )
    assert len(narrow["state_events"]) == 60
    assert narrow["state_events"][0] == ["09:10:00", "idle"]
    assert narrow["state_events"][-1] == ["09:10:59", "active"]
    assert narrow["next_cursor"] is None


def test_budget_not_a_row_count_ends_a_page(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """Hundreds of spans fit on one page; a day of them pages by the character budget."""
    for i in range(1200):
        minute, second = divmod(i * 3, 60)
        add_span(
            source,
            f"2026-09-19 09:{minute:02d}:{second:02d}.000",
            f"2026-09-19 09:{minute:02d}:{second + 2:02d}.000",
            title=f"tab {i}",
        )
    first = connected.call("query_activity", QUERY)
    assert first["total"] == 1200
    assert first["count"] > 400
    assert first["next_cursor"] is not None
    encoded_rows = json.dumps(first["rows"], ensure_ascii=False)
    assert len(encoded_rows) <= ACTIVITY_PAGE_BUDGET
    rows, _, pages = collect(connected, QUERY)
    assert pages <= 3
    assert [row[4] for row in rows] == [f"tab {i} (example.test)" for i in range(1200)]
    assert len({row[0] for row in rows}) == 1200
    assert first["totals"]["A"]["foreground_s"] == 1200 * 2


def test_app_filter_totals_and_summary_only(
    connected: DailyHarness,
    source: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Totals merge overlapping spans per app; app filters by name or bundle substring."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    add_span(
        source, "2026-09-19 09:05:00.000", "2026-09-19 09:12:00.000"
    )  # overlaps: heartbeat row
    add_span(
        source,
        "2026-09-19 09:20:00.000",
        "2026-09-19 09:21:00.000",
        title="Weixin",
        bundle="com.tencent.xinWeChat",
        name="WeChat",
    )
    add_span(
        source,
        "2026-09-19 09:30:00.000",
        "2026-09-19 09:30:07.000",
        title=None,  # type: ignore[arg-type]
        bundle="com.netease.163music",
        name="NetEaseMusic",
    )
    source.execute(
        "INSERT INTO capture(at,lastSeenAt,appBundleID,appName,windowID,title,spanID,text,"
        "imagePath) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-19 09:20:03.000",
            "2026-09-19 09:20:03.000",
            "com.tencent.xinWeChat",
            "WeChat",
            5,
            "Weixin",
            None,
            "蓝苹果 没事 12:03 dawson shi 好",
            None,
        ),
    )
    source.commit()
    emit_event(
        connected.fx.conn,
        type="project.commit_seen",
        ts_epoch_ms=1789809000000,
        payload={
            "repo_path": "/repos/jarvis",
            "commit_sha": "abc",
            "subject": "Fix",
            "committed_at_ms": 1000,
            "actor": "observer",
        },
    )
    everything = connected.call(
        "query_activity", {**SCREEN_QUERY, "sources": ["app", "screen", "git"]}
    )
    assert everything["apps"] == {
        "A": ["Chrome", "com.google.Chrome"],
        "B": ["WeChat", "com.tencent.xinWeChat"],
        "C": ["NetEaseMusic", "com.netease.163music"],
    }
    assert everything["totals"]["A"]["foreground_s"] == 720, "09:00-09:12 once, not 600+420"
    assert everything["totals"]["A"]["spans"] == 2
    assert everything["totals"]["B"] == {
        "foreground_s": 60,
        "spans": 1,
        "screen_rows": 1,
        "first": "09:20:00",
        "last": "09:21:00",
    }
    assert everything["totals"]["C"]["foreground_s"] == 7
    assert everything["total"] == 6

    wechat = connected.call("query_activity", {**SCREEN_QUERY, "app": "wechat"})
    assert wechat["apps"] == {"A": ["WeChat", "com.tencent.xinWeChat"]}
    assert [row[0][0] for row in wechat["rows"]] == ["s", "c"]
    assert wechat["rows"][1][4] == "Weixin — 蓝苹果 没事 12:03 dawson shi 好"
    assert wechat["coverage"]["app"]["app_filter_matches"] == 1
    assert wechat["coverage"]["screen"]["app_filter_matches"] == 1
    by_bundle = connected.call("query_activity", {**QUERY, "app": "163MUSIC"})
    assert by_bundle["rows"] == [
        [by_bundle["rows"][0][0], "09:30:00", "09:30:07", "A", "NetEaseMusic (example.test)"]
    ]
    with_git = connected.call(
        "query_activity", {**QUERY, "sources": ["app", "git"], "app": "chrome"}
    )
    assert all(row[3] != "git" for row in with_git["rows"])
    assert with_git["coverage"]["git"]["reason"].startswith("app filter")

    summary = connected.call("query_activity", {**QUERY, "summary_only": True})
    assert summary["rows"] == []
    assert summary["count"] == 0
    assert summary["total"] == 4
    assert summary["next_cursor"] is None
    assert summary["totals"]["A"]["foreground_s"] == 720

    monkeypatch.setattr(daily_activity, "ACTIVITY_PAGE_BUDGET", 1)
    paged = connected.call("query_activity", QUERY)
    mismatch = connected.call(
        "query_activity", {**QUERY, "app": "wechat", "cursor": paged["next_cursor"]}
    )
    assert mismatch["code"] == "cursor_mismatch"
    assert (
        connected.call("query_activity", {**QUERY, "cursor": "not-a-cursor"})["code"]
        == "invalid_cursor"
    )


def test_totals_name_only_the_sources_queried(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """A source the query did not read has no total: absent, never a zero."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:20:00.000", name="WeChat")
    source.execute(
        "INSERT INTO capture(at,lastSeenAt,appBundleID,appName,windowID,title,spanID,text,"
        "imagePath) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "2026-09-19 09:05:00.000",
            "2026-09-19 09:05:00.000",
            "com.google.Chrome",
            "WeChat",
            5,
            "Weixin",
            None,
            "hi",
            None,
        ),
    )
    source.commit()
    both = connected.call("query_activity", SCREEN_QUERY)
    assert both["totals"]["A"] == {
        "foreground_s": 1200,
        "spans": 1,
        "first": "09:00:00",
        "last": "09:20:00",
        "screen_rows": 1,
    }
    screen_only = connected.call("query_activity", {**QUERY, "sources": ["screen"]})
    assert screen_only["totals"]["A"] == {"screen_rows": 1}
    assert connected.call("query_activity", QUERY)["totals"]["A"] == {
        "foreground_s": 1200,
        "spans": 1,
        "first": "09:00:00",
        "last": "09:20:00",
    }


def test_rows_before_the_page_date_carry_a_date_prefix(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """A local day in Vancouver starts at 07:00Z; a span from the evening before shows its date."""
    add_span(source, "2026-09-19 06:50:00.000", "2026-09-19 07:20:00.000")
    result = connected.call(
        "query_activity",
        {**QUERY, "from": "2026-09-19T00:00:00-07:00", "to": "2026-09-20T00:00:00-07:00"},
    )
    assert result["date"] == "2026-09-19"
    assert result["utc_offset"] == "-07:00"
    assert result["rows"][0][1:3] == ["09-18 23:50:00", "00:20:00"]
    assert result["totals"]["A"]["foreground_s"] == 1200
    assert result["totals"]["A"]["first"] == "00:00:00"


def test_state_at_start_reports_the_standing_state(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """A lock before from with no unlock inside the window explains an empty window."""
    add_event(source, "2026-09-19 08:30:00.000", "start")
    add_event(source, "2026-09-19 08:40:00.000", "idle")
    add_event(source, "2026-09-19 08:45:00.000", "active")
    add_event(source, "2026-09-19 08:50:00.000", "lock")
    add_event(source, "2026-09-19 10:00:00.000", "unlock")
    result = connected.call("query_activity", SCREEN_QUERY)
    assert result["rows"] == []
    assert result["apps"] == {}
    assert result["state_events"] == []
    assert result["state_coverage"]["status"] == "partial"
    assert result["state_at_start"] == [
        ["08:30:00", "start"],
        ["08:45:00", "active"],
        ["08:50:00", "lock"],
    ]
    git_only = connected.call("query_activity", {**QUERY, "sources": ["git"]})
    assert git_only["state_coverage"]["status"] == "unknown"
    assert git_only["state_at_start"] == []


def test_millisecond_boundaries_follow_the_half_open_window(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """GRDB stores milliseconds; [from,to) must hold at both ends for events, captures and spans."""
    add_event(source, "2026-09-19 08:59:59.999", "idle")
    add_event(source, "2026-09-19 09:00:00.000", "active")
    add_event(source, "2026-09-19 09:59:59.999", "lock")
    add_event(source, "2026-09-19 10:00:00.000", "unlock")
    add_capture(source, "2026-09-19 09:00:00.000", "2026-09-19 09:00:00.000", text="at from")
    add_capture(source, "2026-09-19 08:50:00.000", "2026-09-19 09:00:00.000", text="seen at from")
    add_capture(source, "2026-09-19 08:50:00.000", "2026-09-19 08:59:59.999", text="before")
    add_capture(source, "2026-09-19 10:00:00.000", "2026-09-19 10:00:00.000", text="at to")
    add_capture(source, "2026-09-19 09:59:59.999", "2026-09-19 10:00:00.000", text="ends at to")
    add_span(source, "2026-09-19 08:50:00.000", "2026-09-19 09:00:00.000")
    add_span(source, "2026-09-19 10:00:00.000", "2026-09-19 10:10:00.000")
    add_span(source, "2026-09-19 09:59:59.999", "2026-09-19 10:00:00.000")
    for window in (
        {"from": "2026-09-19T09:00:00Z", "to": "2026-09-19T10:00:00Z"},
        {"from": "2026-09-19T02:00:00-07:00", "to": "2026-09-19T12:00:00+02:00"},
        {"from": "2026-09-19T09:00:00.000Z", "to": "2026-09-19T09:59:59.999500Z"},
    ):
        result = connected.call("query_activity", {**SCREEN_QUERY, **window})
        assert [event[1] for event in result["state_events"]] == ["active", "lock"], window
        assert sorted(
            row[4].split(" — ")[1] for row in result["rows"] if row[0].startswith("c")
        ) == sorted(["at from", "seen at from", "ends at to"]), window
        assert len([row for row in result["rows"] if row[0].startswith("s")]) == 1, window
    earlier = connected.call(
        "query_activity",
        {**SCREEN_QUERY, "from": "2026-09-19T08:00:00Z", "to": "2026-09-19T09:00:00Z"},
    )
    assert [event[1] for event in earlier["state_events"]] == ["idle"]
    assert sorted(
        row[4].split(" — ")[1] for row in earlier["rows"] if row[0].startswith("c")
    ) == sorted(["seen at from", "before"])
    assert connected.call("query_activity", {**SCREEN_QUERY, "to": "2026-09-19T09:00:00.000500Z"})[
        "state_events"
    ] == [["09:00:00", "active"]]


def test_text_evidence_outlives_the_image(
    connected: DailyHarness, source: sqlite3.Connection, tmp_path: Path
) -> None:
    """A capture reference outlives its pruned image; text edits still fail closed."""
    image = tmp_path / "captures" / "d" / "1.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"jpeg")
    capture_id = add_capture(
        source, "2026-09-19 09:00:00.000", "2026-09-19 09:01:00.000", text="evidence"
    )
    ref = connected.call("query_activity", SCREEN_QUERY)["rows"][0][0]
    detail = connected.call("read_activity", {"activity_id": ref})
    assert detail["image_available"] is True
    assert detail["image_path"] == str(image)
    knowledge = {
        "statement": "Seen on screen",
        "kind": "fact",
        "basis": "observation",
        "source_refs": detail["source_refs"],
        "request_id": "k-before",
    }
    assert connected.call("save_knowledge", knowledge)["version"] == 1
    image.unlink()  # TimeSink deletes the day folder first ...
    detail = connected.call("read_activity", {"activity_id": ref})
    assert detail["image_available"] is False
    assert detail["image_path"] is None
    assert detail["content"] == "evidence"
    source.execute(
        "UPDATE capture SET imagePath=NULL WHERE id=?", (capture_id,)
    )  # ... then forgets the path
    source.commit()
    detail = connected.call("read_activity", {"activity_id": ref})
    assert detail["image_available"] is False
    assert detail["content"] == "evidence"
    assert connected.call("query_activity", SCREEN_QUERY)["rows"][0][0] == ref
    assert connected.call("save_knowledge", {**knowledge, "request_id": "k-after"})["version"] == 1
    todo = connected.call(
        "create_todo",
        {"title": "Follow up", "source_refs": detail["source_refs"], "request_id": "t"},
    )
    assert "todo_id" in todo
    briefing = connected.call(
        "save_briefing",
        {
            "local_date": "2026-09-19",
            "timezone": "America/Vancouver",
            "content": "Seen on screen",
            "source_refs": detail["source_refs"],
            "coverage": {"screen": "partial"},
            "request_id": "b",
        },
    )
    assert briefing["version"] == 1
    source.execute("UPDATE capture SET text='rewritten' WHERE id=?", (capture_id,))
    source.commit()
    assert connected.call("read_activity", {"activity_id": ref})["code"] == "source_changed"
    assert (
        connected.call("save_knowledge", {**knowledge, "request_id": "k-text"})["code"]
        == "source_changed"
    )


def test_rows_recorded_across_a_break_are_clipped_and_marked(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """Rows written before segments broke on lock/pause/other apps end at the recorded break."""
    add_capture(source, "2026-09-19 09:00:00.000", "2026-09-19 09:50:00.000", text="A")
    add_event(source, "2026-09-19 09:10:00.000", "lock")
    add_event(source, "2026-09-19 09:40:00.000", "unlock")
    add_capture(source, "2026-09-19 09:20:00.000", "2026-09-19 09:45:00.000", text="B")
    source.execute(
        "INSERT INTO span(start,end,appBundleID,appName) VALUES(?,?,?,?)",
        ("2026-09-19 09:30:00.000", "2026-09-19 09:35:00.000", "com.mitchellh.ghostty", "Ghostty"),
    )
    add_capture(source, "2026-09-19 09:46:00.000", "2026-09-19 09:47:00.000", text="C")
    add_span(source, "2026-09-19 09:46:00.000", "2026-09-19 09:47:30.000")  # same app: no break
    source.commit()
    rows = [
        row for row in connected.call("query_activity", SCREEN_QUERY)["rows"] if row[0][0] == "c"
    ]
    assert [(row[4][-1], row[2]) for row in rows] == [
        ("A", "09:10:00!"),
        ("B", "09:30:00!"),
        ("C", "09:47:00"),
    ]
    detail = connected.call("read_activity", {"activity_id": rows[0][0]})
    assert detail["ended_at"] == "2026-09-19T09:10:00.000+00:00"
    assert detail["ended_at_basis"] == "interrupted"
    assert detail["last_seen_at"] == "2026-09-19T09:50:00.000+00:00"
    later = connected.call(
        "query_activity",
        {**SCREEN_QUERY, "from": "2026-09-19T09:40:00Z", "to": "2026-09-19T10:00:00Z"},
    )
    assert [row[4][-1] for row in later["rows"] if row[0][0] == "c"] == ["C"]
