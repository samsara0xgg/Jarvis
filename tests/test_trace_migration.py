"""Tests for memory.trace_migration — v2 -> v3 idempotent upgrade."""
from __future__ import annotations

import sqlite3

import pytest

from memory.trace import TraceLog
from memory.trace_migration import migrate_trace_v2_to_v3

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

V2_SCHEMA = """
CREATE TABLE trace (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    turn_id         INTEGER,
    created_at      TEXT NOT NULL,
    user_text       TEXT,
    assistant_text  TEXT,
    user_emotion    TEXT,
    tts_emotion     TEXT,
    path_taken      TEXT,
    tool_calls      TEXT,
    llm_model       TEXT,
    llm_tokens_in   INTEGER,
    llm_tokens_out  INTEGER,
    latency_ms      INTEGER,
    outcome_signal  INTEGER,
    outcome_at_turn_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trace_session ON trace(session_id, turn_id);
CREATE INDEX IF NOT EXISTS idx_trace_path    ON trace(path_taken, created_at DESC);
"""


def _make_v2_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(V2_SCHEMA)
    conn.commit()
    return conn


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trace'"
    )
    return {row[0] for row in cursor.fetchall()}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFreshInstall:
    def test_v3_schema_present_after_init(self, tmp_path):
        tl = TraceLog(str(tmp_path / "test.db"))
        try:
            conn = tl._get_conn()
            cols = _column_names(conn, "trace")
            assert "user_id" in cols
            assert "input_metadata" in cols
            assert "trigger_source" in cols
            assert "cache_read_input_tokens" in cols
            assert "llm_metadata" in cols
            assert "memory_query_ids" in cols
            assert "prompt_version" in cols
            assert "ttfs_ms" in cols
            assert "latency_breakdown" in cols
            assert "end_reason" in cols
            assert "error" in cols
            assert "finish_reason" in cols
            assert "cost_usd" in cols
            assert len(cols) == 32
        finally:
            tl.close()

    def test_indexes_present_after_fresh_install(self, tmp_path):
        tl = TraceLog(str(tmp_path / "test.db"))
        try:
            conn = tl._get_conn()
            indexes = _index_names(conn)
            assert "idx_trace_session" in indexes
            assert "idx_trace_path" in indexes
            assert "idx_trace_user" in indexes
            assert "idx_trace_outcome" in indexes
            assert "idx_trace_errors" in indexes
        finally:
            tl.close()

    def test_no_backup_table_on_fresh_install(self, tmp_path):
        tl = TraceLog(str(tmp_path / "test.db"))
        try:
            conn = tl._get_conn()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_v2_backup'"
            )
            assert cursor.fetchone() is None
        finally:
            tl.close()


class TestV2ToV3Migration:
    def test_migration_runs_and_returns_true(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        result = migrate_trace_v2_to_v3(conn)
        conn.close()
        assert result is True

    def test_rows_preserved_after_migration(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", 1, "2026-01-01T00:00:00", "hello", "hi"),
        )
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text, outcome_signal) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("s1", 2, "2026-01-01T00:01:00", "bye", "goodbye", 1),
        )
        conn.commit()
        migrate_trace_v2_to_v3(conn)

        rows = conn.execute("SELECT * FROM trace ORDER BY id").fetchall()
        assert len(rows) == 2
        r0 = dict(rows[0])
        assert r0["user_text"] == "hello"
        assert r0["assistant_text"] == "hi"
        assert r0["session_id"] == "s1"
        assert r0["turn_id"] == 1
        r1 = dict(rows[1])
        assert r1["outcome_signal"] == 1
        conn.close()

    def test_user_id_defaults_to_default_user(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", 1, "2026-01-01T00:00:00", "hello", "hi"),
        )
        conn.commit()
        migrate_trace_v2_to_v3(conn)

        row = conn.execute("SELECT user_id FROM trace WHERE session_id='s1'").fetchone()
        assert row["user_id"] == "default_user"
        conn.close()

    def test_new_columns_are_null(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", 1, "2026-01-01T00:00:00", "hello", "hi"),
        )
        conn.commit()
        migrate_trace_v2_to_v3(conn)

        row = dict(conn.execute("SELECT * FROM trace").fetchone())
        for col in ("input_metadata", "trigger_source", "parent_trace_id", "intent_route_score",
                    "llm_metadata", "memory_query_ids", "prompt_version", "ttfs_ms",
                    "latency_breakdown", "end_reason", "error", "finish_reason",
                    "cost_usd", "cache_read_input_tokens"):
            assert row[col] is None, f"expected {col} to be NULL, got {row[col]!r}"
        conn.close()

    def test_backup_table_exists_after_migration(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        migrate_trace_v2_to_v3(conn)

        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_v2_backup'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_v3_schema_present_after_migration(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.row_factory = sqlite3.Row
        migrate_trace_v2_to_v3(conn)
        cols = _column_names(conn, "trace")
        assert "user_id" in cols
        assert len(cols) == 32
        conn.close()

    def test_indexes_present_after_migration(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.row_factory = sqlite3.Row
        migrate_trace_v2_to_v3(conn)
        indexes = _index_names(conn)
        assert "idx_trace_session" in indexes
        assert "idx_trace_path" in indexes
        assert "idx_trace_user" in indexes
        assert "idx_trace_outcome" in indexes
        assert "idx_trace_errors" in indexes
        conn.close()

    def test_tracelog_init_triggers_migration(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        # Build a v2 DB with one row
        conn = _make_v2_conn(db_path)
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", 1, "2026-01-01T00:00:00", "hi", "hello"),
        )
        conn.commit()
        conn.close()

        # TraceLog init should run migration automatically
        tl = TraceLog(db_path)
        try:
            conn2 = tl._get_conn()
            cols = _column_names(conn2, "trace")
            assert "user_id" in cols
            row = conn2.execute("SELECT user_id, user_text FROM trace").fetchone()
            assert dict(row)["user_id"] == "default_user"
            assert dict(row)["user_text"] == "hi"
        finally:
            tl.close()


class TestMigrationIdempotent:
    def test_second_call_returns_false(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        first = migrate_trace_v2_to_v3(conn)
        second = migrate_trace_v2_to_v3(conn)
        assert first is True
        assert second is False
        conn.close()

    def test_second_call_does_not_change_data(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        conn = _make_v2_conn(db_path)
        conn.execute(
            "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text) "
            "VALUES (?, ?, ?, ?, ?)",
            ("s1", 1, "2026-01-01T00:00:00", "hello", "hi"),
        )
        conn.commit()
        migrate_trace_v2_to_v3(conn)
        migrate_trace_v2_to_v3(conn)
        count = conn.execute("SELECT COUNT(*) FROM trace").fetchone()[0]
        assert count == 1
        conn.close()

    def test_tracelog_init_idempotent_on_v3(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        tl1 = TraceLog(db_path)
        tl1.close()
        # Second init on same v3 DB must not raise
        tl2 = TraceLog(db_path)
        tl2.close()
