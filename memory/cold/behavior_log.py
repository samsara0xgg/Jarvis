"""Append-only behavior event log for usage pattern analysis (T2).

Records skill calls, conversations, suggestions, and corrections
in a SQLite table. Designed for T2 behavior learning to consume.
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

class BehaviorLog:
    """Append-only behavior event store.

    Args:
        db_path: Path to the SQLite database file.
    """
    def __init__(self, db_path: str | Path = "data/memory/jarvis_memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS behavior_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                detail      TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_behavior_user_time "
            "ON behavior_log(user_id, timestamp DESC)"
        )
        conn.commit()

    def log(self, user_id: str, event_type: str, detail: dict[str, Any] | None = None) -> None:
        """Append a behavior event."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO behavior_log (timestamp, user_id, event_type, detail) VALUES (?, ?, ?, ?)",
            (datetime.now().isoformat(), user_id, event_type,
             json.dumps(detail or {}, ensure_ascii=False)),
        )
        conn.commit()

    def get_events(self, user_id: str, event_type: str | None = None,
                   since_days: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Query behavior events for a user, newest first."""
        conn = self._get_conn()
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if since_days is not None:
            clauses.append("timestamp >= datetime('now', ?)")
            params.append(f"-{since_days} days")
        where = " AND ".join(clauses)
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM behavior_log WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
            except (json.JSONDecodeError, TypeError):
                d["detail"] = {}
            results.append(d)
        return results

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
