"""The attention log as a test reads it: every logged decision, with its JSON columns decoded.

Production only writes this log (``job_ledger.log_decision``, ``note_delivery``); no code lists it.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, Any

from jarvis.state.memory_db import open_memory_db

if TYPE_CHECKING:
    from pathlib import Path


def attention_rows(path: Path, source: str | None = None) -> list[dict[str, Any]]:
    """Every logged decision, oldest first, with delivery and feedback read back as JSON."""
    conn = open_memory_db(path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM attention_log WHERE ? IS NULL OR source = ? ORDER BY at, rowid",
                (source, source),
            ).fetchall()
        except sqlite3.OperationalError:  # no table yet: nothing was ever logged
            return []
    finally:
        conn.close()
    return [
        {
            **dict(row),
            "delivery": json.loads(row["delivery"]),
            "feedback": json.loads(row["feedback_json"]),
        }
        for row in rows
    ]
