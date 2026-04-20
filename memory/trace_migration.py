"""Idempotent migration from trace v2 (16-col) to v3 (31-col).

v3 adds user_id, all new routing/LLM/performance/lifecycle columns, 2 CHECK
constraints, and 3 additional indexes. Because SQLite cannot add CHECK
constraints via ALTER TABLE, the migration renames the old table and rebuilds.

The old table is kept as trace_v2_backup for operator verification — drop it
manually after confirming v3 is healthy.
"""
from __future__ import annotations

import logging
import sqlite3

LOGGER = logging.getLogger(__name__)

V3_SCHEMA_SQL = """
CREATE TABLE trace (
  id                        INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id                TEXT    NOT NULL,
  turn_id                   INTEGER NOT NULL,
  user_id                   TEXT    NOT NULL DEFAULT 'default_user',
  created_at                TEXT    NOT NULL,

  user_text                 TEXT,
  assistant_text            TEXT,
  user_emotion              TEXT,
  tts_emotion               TEXT,
  input_metadata            TEXT,

  trigger_source            TEXT,
  parent_trace_id           INTEGER,
  path_taken                TEXT,
  intent_route_score        REAL,

  tool_calls                TEXT,

  llm_model                 TEXT,
  llm_tokens_in             INTEGER,
  llm_tokens_out            INTEGER,
  cache_read_input_tokens   INTEGER,
  llm_metadata              TEXT,

  memory_query_ids          TEXT,

  prompt_version            TEXT,

  latency_ms                INTEGER,
  ttfs_ms                   INTEGER,
  latency_breakdown         TEXT,

  end_reason                TEXT,
  error                     TEXT,
  finish_reason             TEXT,
  cost_usd                  REAL,
  outcome_signal            INTEGER,
  outcome_at_turn_id        INTEGER,

  CHECK (outcome_signal IS NULL OR outcome_signal IN (-1, 0, 1)),
  CHECK (end_reason IS NULL OR end_reason IN ('success', 'interrupted', 'error', 'timeout', 'cancelled'))
);
CREATE INDEX idx_trace_session ON trace(session_id, turn_id);
CREATE INDEX idx_trace_path    ON trace(path_taken, created_at DESC);
CREATE INDEX idx_trace_user    ON trace(user_id, created_at DESC);
CREATE INDEX idx_trace_outcome ON trace(outcome_signal, created_at DESC)
  WHERE outcome_signal IS NOT NULL;
CREATE INDEX idx_trace_errors  ON trace(created_at DESC)
  WHERE error IS NOT NULL;
"""


def migrate_trace_v2_to_v3(conn: sqlite3.Connection) -> bool:
    """Upgrade trace table from v2 (16 cols) to v3 (31 cols).

    Idempotent: returns False and does nothing if already v3. Detects v3 by
    checking for the 'user_id' column in the existing trace table.

    Strategy: rename old table to trace_v2_backup, create v3 schema, copy rows
    with 'default_user' for user_id and NULL for all new columns, create
    indexes. The backup table is intentionally left for operator review.

    Args:
        conn: Open SQLite connection to the database containing the trace table.

    Returns:
        True if migration ran, False if the table was already v3.
    """
    cursor = conn.execute("PRAGMA table_info(trace)")
    columns = {row[1] for row in cursor.fetchall()}
    if "user_id" in columns:
        LOGGER.info("trace table already v3 — migration skipped")
        return False

    LOGGER.info("trace table is v2 — starting migration to v3")

    # Drop any indexes on the old table before renaming, because SQLite keeps
    # index names global — they survive the rename and would conflict when the
    # v3 CREATE INDEX statements run.
    old_indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trace'"
    ).fetchall()
    drop_stmts = "".join(f"DROP INDEX IF EXISTS {row[0]};" for row in old_indexes)
    if drop_stmts:
        conn.executescript(drop_stmts)
        LOGGER.info("dropped %d old indexes from trace table", len(old_indexes))

    conn.executescript("ALTER TABLE trace RENAME TO trace_v2_backup;")
    LOGGER.info("renamed trace -> trace_v2_backup")

    conn.executescript(V3_SCHEMA_SQL)
    LOGGER.info("v3 schema created")

    conn.executescript("""
        INSERT INTO trace (
            id, session_id, turn_id, user_id, created_at,
            user_text, assistant_text, user_emotion, tts_emotion,
            path_taken, tool_calls, llm_model,
            llm_tokens_in, llm_tokens_out, latency_ms,
            outcome_signal, outcome_at_turn_id
        )
        SELECT
            id, session_id, turn_id, 'default_user', created_at,
            user_text, assistant_text, user_emotion, tts_emotion,
            path_taken, tool_calls, llm_model,
            llm_tokens_in, llm_tokens_out, latency_ms,
            outcome_signal, outcome_at_turn_id
        FROM trace_v2_backup;
    """)
    LOGGER.info("copied rows from trace_v2_backup into new trace table")

    conn.commit()
    LOGGER.info("migration to v3 complete — trace_v2_backup retained for operator review")
    return True
