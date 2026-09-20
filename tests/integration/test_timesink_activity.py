"""Read-only TimeSink integration through Jarvis's real tool dispatcher."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import _register_workers, _timesink_db_path
from jarvis.state.event_log import emit_event
from tests.integration.test_daily_tools import DailyHarness

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

QUERY = {"from": "2026-09-19T09:00:00Z", "to": "2026-09-19T10:00:00Z", "sources": ["app"]}


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
def connected(tmp_path: Path, source: sqlite3.Connection) -> Iterator[DailyHarness]:
    """Wire TimeSink into the default registry with actual on-disk stores."""
    del source
    harness = DailyHarness(tmp_path, timesink_path=tmp_path / "timesink.sqlite")
    yield harness
    harness.fx.close()


def add_span(conn: sqlite3.Connection, start: str, end: str, *, title: str = "Jarvis notes") -> int:
    """Write source data exactly as the upstream producer would."""
    cursor = conn.execute(
        "INSERT INTO span(start,end,appBundleID,appName,title,url,domain) VALUES(?,?,?,?,?,?,?)",
        (
            start,
            end,
            "com.google.Chrome",
            "Chrome",
            title,
            "https://example.test/docs",
            "example.test",
        ),
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
        (at, last_seen, "com.mitchellh.ghostty", "Ghostty", 13117, "cc | rules", None, text, image),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_overlap_utc_boundaries_and_readonly(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """Include the clipped tail of earlier spans, exclude exact boundaries and zero spans."""
    add_span(source, "2026-09-19 08:50:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 08:00:00.000", "2026-09-19 09:00:00.000")
    add_span(source, "2026-09-19 10:00:00.000", "2026-09-19 10:10:00.000")
    add_span(source, "2026-09-19 09:15:00.000", "2026-09-19 09:15:00.000")
    before = source.execute("SELECT * FROM span").fetchall()
    result = connected.call("query_activity", QUERY)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["duration_seconds_in_window"] == 600
    assert item["occurred_at"] == "2026-09-19T08:50:00.000+00:00"
    assert item["observed_at"] is None
    assert item["project"] is None
    assert result["coverage"]["app"]["status"] == "partial"
    assert result["coverage"]["app"]["gap_reason"] == "unknown"
    detail = connected.call("read_activity", {"activity_id": item["id"]})
    assert json.loads(detail["content"])["url"] == "https://example.test/docs"
    assert source.execute("SELECT * FROM span").fetchall() == before
    assert connected.call("query_activity", {**QUERY, "project": "/repos/jarvis"})["items"] == []


def test_paging_excludes_new_rows_and_detects_corrections(
    connected: DailyHarness,
    source: sqlite3.Connection,
) -> None:
    """An append watermark alone cannot freeze a TimeSink row's mutable end time."""
    first_id = add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 09:20:00.000", "2026-09-19 09:30:00.000")
    args = {**QUERY, "limit": 1}
    page = connected.call("query_activity", args)
    add_span(source, "2026-09-19 09:15:00.000", "2026-09-19 09:16:00.000")
    second = connected.call("query_activity", {**args, "cursor": page["next_cursor"]})
    assert second["next_cursor"] is None
    assert second["items"][0]["occurred_at"] == "2026-09-19T09:20:00.000+00:00"
    source.execute("UPDATE span SET end='2026-09-19 09:05:00.000' WHERE id=?", (first_id,))
    source.commit()
    assert (
        connected.call("query_activity", {**args, "cursor": page["next_cursor"]})["code"]
        == "invalid_cursor"
    )
    assert (
        connected.call("read_activity", {"activity_id": page["items"][0]["id"]})["code"]
        == "source_changed"
    )
    fresh = connected.call("query_activity", QUERY)
    assert fresh["count"] == 3
    assert fresh["items"][0]["duration_seconds_in_window"] == 300


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
    args = {**QUERY, "sources": ["app", "git", "screen"], "limit": 1}
    page = connected.call("query_activity", args)
    assert page["items"][0]["source"] == "app"
    assert page["coverage"]["screen"]["status"] == "partial"
    following = connected.call("query_activity", {**args, "cursor": page["next_cursor"]})
    assert following["items"][0]["source"] == "git"
    assert following["items"][0]["occurred_at"].startswith("1970")
    # Stop the source first, so its WAL is checkpointed before moving the file.
    source.close()
    (tmp_path / "timesink.sqlite").rename(tmp_path / "offline.sqlite")
    unavailable = connected.call("query_activity", args)
    assert unavailable["coverage"]["app"]["status"] == "unavailable"
    assert unavailable["items"][0]["source"] == "git"
    assert not (tmp_path / "timesink.sqlite").exists()


def test_exact_long_detail_and_revision_refs(
    connected: DailyHarness,
    source: sqlite3.Connection,
) -> None:
    """Bounded summaries retain exact originals and usable briefing/knowledge provenance."""
    title = 'notes\n"\\世界' * 3000
    identity = add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000", title=title)
    item = connected.call("query_activity", QUERY)["items"][0]
    assert len(item["summary"]) == 300
    args: dict[str, Any] = {"activity_id": item["id"]}
    content = ""
    while True:
        page = connected.call("read_activity", args)
        assert page["offset"] == len(content)
        content += page["content"]
        if page["next_cursor"] is None:
            break
        args["cursor"] = page["next_cursor"]
    assert json.loads(content)["title"] == title
    knowledge = {
        "statement": "Read documentation",
        "kind": "fact",
        "basis": "observation",
        "source_refs": item["source_refs"],
        "request_id": "knowledge",
    }
    receipt = connected.call("save_knowledge", knowledge)
    assert receipt["version"] == 1
    todo = connected.call(
        "create_todo",
        {"title": "Follow up", "source_refs": item["source_refs"], "request_id": "todo"},
    )
    briefing = connected.call(
        "save_briefing",
        {
            "local_date": "2026-09-19",
            "timezone": "America/Vancouver",
            "content": "Read docs",
            "source_refs": item["source_refs"],
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


def test_database_identity_pins_refs_and_cursors(
    connected: DailyHarness,
    source: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    """Replacing a DB with identical rows must not silently reuse old evidence IDs."""
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    add_span(source, "2026-09-19 09:20:00.000", "2026-09-19 09:30:00.000")
    first = connected.call("query_activity", {**QUERY, "limit": 1})
    source.close()
    path = tmp_path / "timesink.sqlite"
    path.rename(tmp_path / "old.sqlite")
    with (
        closing(sqlite3.connect(tmp_path / "old.sqlite")) as old,
        closing(sqlite3.connect(path)) as new,
    ):
        old.backup(new)
    assert (
        connected.call("read_activity", {"activity_id": first["items"][0]["id"]})["code"]
        == "source_changed"
    )
    assert (
        connected.call("query_activity", {**QUERY, "limit": 1, "cursor": first["next_cursor"]})[
            "code"
        ]
        == "invalid_cursor"
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
    assert result["items"][0]["duration_seconds_in_window"] == 3600
    add_span(source, "2026-09-19 09:00:00.001", "2026-09-19 09:00:01.000")
    assert (
        connected.call("query_activity", {**QUERY, "to": "2026-09-19T09:00:00.001Z"})["count"] == 0
    )
    assert (
        connected.call("query_activity", {**QUERY, "to": "2026-09-19T09:00:00.001500Z"})["count"]
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
        assert result["count"] == 1
        assert h.call("read_activity", {"activity_id": result["items"][0]["id"]})["complete"]
    finally:
        workers.stop()
        h.fx.close()


SCREEN_QUERY = {**QUERY, "sources": ["app", "screen"]}


def test_screen_captures_state_events_and_full_text(
    connected: DailyHarness, source: sqlite3.Connection
) -> None:
    """A screen row is one content stretch; its full OCR text and the gap reasons are readable."""
    long_text = "OCR line " * 2000
    add_span(source, "2026-09-19 09:00:00.000", "2026-09-19 09:10:00.000")
    capture_id = add_capture(
        source, "2026-09-19 08:59:00.000", "2026-09-19 09:04:00.000", text=long_text
    )
    add_capture(
        source, "2026-09-19 09:30:00.000", "2026-09-19 09:31:00.000", text="short", image=None
    )
    add_capture(source, "2026-09-19 10:00:00.000", "2026-09-19 10:05:00.000", text="later")
    source.execute(
        "INSERT INTO stateEvent(at,kind) VALUES(?,?)", ("2026-09-19 09:12:00.000", "idle")
    )
    source.execute(
        "INSERT INTO stateEvent(at,kind) VALUES(?,?)", ("2026-09-19 09:40:00.000", "lock")
    )
    source.commit()

    result = connected.call("query_activity", SCREEN_QUERY)
    assert [item["source"] for item in result["items"]] == ["screen", "app", "screen"]
    first = result["items"][0]
    assert first["kind"] == "screen.capture"
    assert first["occurred_at"] == "2026-09-19T08:59:00.000+00:00"
    assert first["ended_at"] == "2026-09-19T09:04:00.000+00:00"
    assert first["duration_seconds_in_window"] == 240
    assert first["summary"].startswith("Ghostty: cc | rules — OCR line")
    assert len(first["summary"]) == 300
    assert first["image_available"] is True
    assert result["items"][2]["image_available"] is False
    assert result["coverage"]["screen"]["status"] == "partial"
    assert result["coverage"]["screen"]["observations_in_window"] == 2
    assert result["state_events"] == [
        {"at": "2026-09-19T09:12:00.000+00:00", "kind": "idle"},
        {"at": "2026-09-19T09:40:00.000+00:00", "kind": "lock"},
    ]

    args: dict[str, Any] = {"activity_id": first["id"]}
    content = ""
    while True:
        page = connected.call("read_activity", args)
        assert page["content_format"] == "text"
        assert page["image_path"].endswith("/captures/d/1.jpg")
        content += page["content"]
        if page["next_cursor"] is None:
            break
        args["cursor"] = page["next_cursor"]
    assert content == long_text

    knowledge = connected.call(
        "save_knowledge",
        {
            "statement": "Was editing capture rules",
            "kind": "fact",
            "basis": "observation",
            "source_refs": first["source_refs"],
            "request_id": "k1",
        },
    )
    assert knowledge["version"] == 1
    source.execute(
        "UPDATE capture SET lastSeenAt='2026-09-19 09:06:00.000' WHERE id=?", (capture_id,)
    )
    source.commit()
    assert connected.call("read_activity", {"activity_id": first["id"]})["code"] == "source_changed"
    assert connected.call("query_activity", {**SCREEN_QUERY, "project": "/repos/x"})["items"] == []


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
        assert result["count"] == 1
        assert result["coverage"]["app"]["status"] == "partial"
        assert result["coverage"]["screen"]["status"] == "unavailable"
        assert result["state_events"] == []
    finally:
        h.fx.close()
