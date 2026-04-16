"""Structured per-turn trace table for v2.

Records every conversation turn with path, latency, tool calls,
emotions, and outcome signals. Replaces the old behavior_log for
richer v2 analytics. Shares the same SQLite database as MemoryStore
(WAL mode, thread-local connections).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class TraceLog:
    """Per-turn trace store for v2 conversation analytics.

    Args:
        db_path: Path to the shared SQLite database file.
    """

    def __init__(self, db_path: str | Path = "data/memory/jarvis_memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a thread-local SQLite connection (WAL mode for concurrent access)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        """Create the trace table and indexes if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trace (
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
            CREATE INDEX IF NOT EXISTS idx_trace_session
                ON trace(session_id, turn_id);
            CREATE INDEX IF NOT EXISTS idx_trace_path
                ON trace(path_taken, created_at DESC);
        """)
        conn.commit()

    def log_turn(
        self,
        *,
        session_id: str,
        turn_id: int,
        user_text: str,
        assistant_text: str,
        user_emotion: str | None = None,
        tts_emotion: str | None = None,
        path_taken: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        llm_model: str | None = None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        latency_ms: int | None = None,
    ) -> int:
        """Log a single conversation turn.

        Args:
            session_id: Unique session identifier.
            turn_id: Monotonic turn counter within the session.
            user_text: Raw user utterance.
            assistant_text: Assistant response text.
            user_emotion: Detected user emotion (optional).
            tts_emotion: Emotion mapped for TTS (optional).
            path_taken: Routing path (e.g. 'cloud', 'local', 'farewell').
            tool_calls: List of tool call dicts (optional).
            llm_model: LLM model used (optional).
            llm_tokens_in: Input token count (optional).
            llm_tokens_out: Output token count (optional).
            latency_ms: End-to-end latency in milliseconds (optional).

        Returns:
            The auto-generated trace row ID.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO trace "
            "(session_id, turn_id, created_at, user_text, assistant_text, "
            "user_emotion, tts_emotion, path_taken, tool_calls, "
            "llm_model, llm_tokens_in, llm_tokens_out, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                turn_id,
                datetime.now().isoformat(),
                user_text,
                assistant_text,
                user_emotion,
                tts_emotion,
                path_taken,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                llm_model,
                llm_tokens_in,
                llm_tokens_out,
                latency_ms,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        LOGGER.debug("Trace logged: session=%s turn=%d id=%d", session_id, turn_id, row_id)
        return row_id

    def update_outcome(self, trace_id: int, signal: int) -> None:
        """Set the outcome signal for a trace row.

        Args:
            trace_id: The trace row ID to update.
            signal: Outcome signal (e.g. 1 = positive, -1 = negative).
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE trace SET outcome_signal = ? WHERE id = ?",
            (signal, trace_id),
        )
        conn.commit()

    def query_for_observer(self, trace_id: int) -> dict[str, Any] | None:
        """Retrieve the fields needed by the Observer for a given trace.

        Args:
            trace_id: The trace row ID to look up.

        Returns:
            A dict with user_text, assistant_text, tool_calls (as list),
            and user_emotion, or None if not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT user_text, assistant_text, tool_calls, user_emotion "
            "FROM trace WHERE id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        # Deserialize tool_calls from JSON text to list
        tc = result.get("tool_calls")
        if tc:
            try:
                result["tool_calls"] = json.loads(tc)
            except (json.JSONDecodeError, TypeError):
                result["tool_calls"] = []
        else:
            result["tool_calls"] = []
        return result

    def query_cloud_traces(self, days: int = 7) -> list[dict[str, Any]]:
        """Query recent cloud-path traces for hotspot detection.

        Returns traces where path_taken = 'cloud' and the outcome is
        either not yet set or non-negative (i.e. not explicitly bad).

        Args:
            days: Number of days to look back.

        Returns:
            List of trace dicts, newest first.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM trace "
            "WHERE path_taken = 'cloud' "
            "AND (outcome_signal IS NULL OR outcome_signal >= 0) "
            "AND created_at > datetime('now', ?) "
            "ORDER BY created_at DESC",
            (f"-{days} days",),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            tc = d.get("tool_calls")
            if tc:
                try:
                    d["tool_calls"] = json.loads(tc)
                except (json.JSONDecodeError, TypeError):
                    d["tool_calls"] = []
            else:
                d["tool_calls"] = []
            results.append(d)
        return results

    def close(self) -> None:
        """Close the current thread's database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
