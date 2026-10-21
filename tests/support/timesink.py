"""A live TimeSink store with the real schema, and the rows a producer would write."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def source(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A live WAL writer using the real TimeSink schema and GRDB timestamp encoding."""
    conn = sqlite3.connect(tmp_path / "timesink.sqlite")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE span (id INTEGER PRIMARY KEY AUTOINCREMENT, start DATETIME NOT NULL, "
        "end DATETIME NOT NULL, appBundleID TEXT NOT NULL, appName TEXT NOT NULL, "
        "title TEXT, url TEXT, domain TEXT, document TEXT)"
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
