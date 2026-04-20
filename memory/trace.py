"""Structured per-turn trace store — v3 schema (31 columns).

Records every conversation turn with identity, routing path, tool calls,
LLM metadata, memory query IDs, latency breakdowns, and outcome signals.
Serves three downstreams: Phase 3 auto-skill-learning, MCP debug queries,
and long-term cost/latency analytics.

Migration: existing v2 databases are upgraded automatically on first init.
Fresh installs receive the v3 schema directly.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from memory.trace_migration import V3_SCHEMA_SQL, migrate_trace_v2_to_v3

LOGGER = logging.getLogger(__name__)


class TriggerSource(str, Enum):
    """How the conversation turn was initiated."""

    WAKE_WORD = "wake_word"
    CONTINUATION = "continuation"
    WEB_TEXT = "web_text"
    WEB_VOICE = "web_voice"
    PROACTIVE = "proactive"
    TEST = "test"


class PathTaken(str, Enum):
    """Which routing branch handled the turn."""

    UNKNOWN = "unknown"
    RESUME = "resume"
    FAREWELL = "farewell"
    MEMORY_SHORTCUT = "memory_shortcut"
    KEYWORD_RULE = "keyword_rule"
    MEMORY_L1 = "memory_l1"
    LOCAL = "local"
    CLOUD = "cloud"


class EndReason(str, Enum):
    """Why the entire turn ended (Jarvis-level)."""

    SUCCESS = "success"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class FinishReason(str, Enum):
    """Why LLM generation terminated (provider-level)."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"


class TraceLog:
    """Per-turn trace store — v3 schema.

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
        """Create or migrate the trace table and indexes.

        Fresh install: creates v3 schema directly.
        Existing v2: delegates to migrate_trace_v2_to_v3.
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trace'"
        )
        exists = cursor.fetchone() is not None

        if not exists:
            conn.executescript(V3_SCHEMA_SQL)
            conn.commit()
            LOGGER.info("trace v3 schema created (fresh install)")
            return

        migrate_trace_v2_to_v3(conn)

    def log_turn(
        self,
        *,
        # --- Identity ---
        session_id: str,
        turn_id: int,
        user_id: str = "default_user",
        # --- Input / Output ---
        user_text: str,
        assistant_text: str,
        user_emotion: str | None = None,
        tts_emotion: str | None = None,
        input_metadata: dict | None = None,
        # --- Triggering / Routing ---
        trigger_source: str | None = None,
        parent_trace_id: int | None = None,
        path_taken: str | None = None,
        intent_route_score: float | None = None,
        # --- Tools ---
        tool_calls: list[dict] | None = None,
        # --- LLM ---
        llm_model: str | None = None,
        llm_tokens_in: int | None = None,
        llm_tokens_out: int | None = None,
        cache_read_input_tokens: int | None = None,
        llm_metadata: dict | None = None,
        # --- Memory ---
        memory_query_ids: dict | None = None,
        # --- Context ---
        prompt_version: str | None = None,
        # --- Performance ---
        latency_ms: int | None = None,
        ttfs_ms: int | None = None,
        latency_breakdown: dict | None = None,
        # --- Lifecycle ---
        end_reason: str | None = None,
        error: str | None = None,
        finish_reason: str | None = None,
        cost_usd: float | None = None,
    ) -> int:
        """Log a single conversation turn. Returns the inserted row ID.

        Args:
            session_id: Unique session identifier.
            turn_id: Monotonic turn counter within the session.
            user_id: User identifier. Defaults to 'default_user'.
            user_text: Normalized user utterance (post-ASR correction).
            assistant_text: Assistant final response text.
            user_emotion: SenseVoice detected emotion (happy/angry/sad/neutral/...).
            tts_emotion: TTS playback intended emotion.
            input_metadata: JSON-serializable dict. See V3 schema doc for keys:
                asr_text_raw, asr_confidence, vad_duration_ms, audio_path.
            trigger_source: How the turn was initiated. Use TriggerSource enum.
            parent_trace_id: FK to trace.id of parent turn, or None.
            path_taken: Routing branch. Use PathTaken enum values.
            intent_route_score: float | None
                Router confidence [0, 1]. NOTE: currently LLM self-reported
                (not calibrated logprob). Reliable for relative ordering,
                unreliable as an absolute probability. Switch to logprob
                when router layer exposes it.
            tool_calls: List of tool invocation dicts: [{name, args, result, ms}].
            llm_model: Model identifier (e.g. 'grok-4.20'). NULL when path != cloud.
            llm_tokens_in: Prompt token count.
            llm_tokens_out: Completion token count.
            cache_read_input_tokens: Prompt cache hit tokens (all providers).
            llm_metadata: JSON-serializable dict. See V3 schema doc for keys.
            memory_query_ids: JSON-serializable dict with observation_ids and scores.
            prompt_version: SHA-256 hash prefix (16 chars) of system prompt.
            latency_ms: Total end-to-end latency in milliseconds.
            ttfs_ms: Time to first sound in milliseconds (user-perceived).
            latency_breakdown: JSON-serializable dict. See V3 schema doc for keys.
            end_reason: Why the turn ended (Jarvis-level). Use EndReason enum.
            error: Exception message and short traceback on failure.
            finish_reason: LLM generation stop reason. Use FinishReason enum.
            cost_usd: Computed cost from llm_pricing * tokens.

        Returns:
            The auto-generated trace row ID (lastrowid).
        """
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO trace ("
            "session_id, turn_id, user_id, created_at, "
            "user_text, assistant_text, user_emotion, tts_emotion, input_metadata, "
            "trigger_source, parent_trace_id, path_taken, intent_route_score, "
            "tool_calls, "
            "llm_model, llm_tokens_in, llm_tokens_out, cache_read_input_tokens, llm_metadata, "
            "memory_query_ids, "
            "prompt_version, "
            "latency_ms, ttfs_ms, latency_breakdown, "
            "end_reason, error, finish_reason, cost_usd"
            ") VALUES ("
            "?, ?, ?, ?, "
            "?, ?, ?, ?, ?, "
            "?, ?, ?, ?, "
            "?, "
            "?, ?, ?, ?, ?, "
            "?, "
            "?, "
            "?, ?, ?, "
            "?, ?, ?, ?"
            ")",
            (
                session_id,
                turn_id,
                user_id,
                datetime.now().isoformat(),
                user_text,
                assistant_text,
                user_emotion,
                tts_emotion,
                json.dumps(input_metadata, ensure_ascii=False) if input_metadata is not None else None,
                trigger_source,
                parent_trace_id,
                path_taken,
                intent_route_score,
                json.dumps(tool_calls, ensure_ascii=False) if tool_calls is not None else None,
                llm_model,
                llm_tokens_in,
                llm_tokens_out,
                cache_read_input_tokens,
                json.dumps(llm_metadata, ensure_ascii=False) if llm_metadata is not None else None,
                json.dumps(memory_query_ids, ensure_ascii=False) if memory_query_ids is not None else None,
                prompt_version,
                latency_ms,
                ttfs_ms,
                json.dumps(latency_breakdown, ensure_ascii=False) if latency_breakdown is not None else None,
                end_reason,
                error,
                finish_reason,
                cost_usd,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        LOGGER.debug("trace logged: session=%s turn=%d id=%d", session_id, turn_id, row_id)
        return row_id

    def update_ttfs(self, trace_id: int, ttfs_ms: int) -> None:
        """Patch ttfs_ms on a previously-logged row.

        Used when TTS playback completes asynchronously after log_turn
        already wrote the initial row (typical for local-path turns where
        output_fn dispatches to a background TTS executor and the first
        audio chunk arrives long after the trace row is committed).

        Mirrors the value into latency_breakdown.tts_first_ms in the JSON
        column so the two views stay consistent.

        Args:
            trace_id: Row id returned by ``log_turn``.
            ttfs_ms: Time to first sound, in ms (>= 0).
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT latency_breakdown FROM trace WHERE id = ?",
            (trace_id,),
        ).fetchone()
        if row is None:
            return
        breakdown_text = row["latency_breakdown"]
        breakdown: dict[str, Any] = {}
        if breakdown_text:
            try:
                breakdown = json.loads(breakdown_text)
            except (json.JSONDecodeError, TypeError):
                breakdown = {}
        breakdown["tts_first_ms"] = ttfs_ms
        conn.execute(
            "UPDATE trace SET ttfs_ms = ?, latency_breakdown = ? WHERE id = ?",
            (
                ttfs_ms,
                json.dumps(breakdown, ensure_ascii=False),
                trace_id,
            ),
        )
        conn.commit()
        LOGGER.debug("trace ttfs_ms patched: id=%d ttfs=%d", trace_id, ttfs_ms)

    def update_outcome(
        self,
        trace_id: int,
        signal: int,
        at_turn_id: int | None = None,
    ) -> None:
        """Set outcome signal (and optionally the inferring turn) for a trace row.

        Passing None for at_turn_id leaves any existing outcome_at_turn_id unchanged.

        Args:
            trace_id: Trace row to update.
            signal: -1 / 0 / +1.
            at_turn_id: The trace.id of the turn from which this outcome was inferred.
        """
        conn = self._get_conn()
        conn.execute(
            "UPDATE trace SET outcome_signal=?, outcome_at_turn_id=COALESCE(?, outcome_at_turn_id) WHERE id=?",
            (signal, at_turn_id, trace_id),
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
            for col in ("tool_calls", "input_metadata", "llm_metadata", "memory_query_ids", "latency_breakdown"):
                val = d.get(col)
                if val:
                    try:
                        d[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        d[col] = None
                else:
                    d[col] = [] if col == "tool_calls" else None
            results.append(d)
        return results

    def query_for_debug(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        hours: int = 24,
        only_errors: bool = False,
        only_interrupted: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch traces matching filters, newest first. Deserializes JSON columns.

        Args:
            session_id: Filter to a specific session, or None for all sessions.
            user_id: Filter to a specific user, or None for all users.
            hours: How many hours back to query.
            only_errors: If True, return only rows where error IS NOT NULL.
            only_interrupted: If True, return only rows where end_reason = 'interrupted'.

        Returns:
            List of trace dicts with all 5 JSON columns deserialized. Newest first.
        """
        conn = self._get_conn()
        conditions = ["created_at > datetime('now', ?)"]
        params: list[Any] = [f"-{hours} hours"]

        if session_id is not None:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        if only_errors:
            conditions.append("error IS NOT NULL")
        if only_interrupted:
            conditions.append("end_reason = 'interrupted'")

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM trace WHERE {where} ORDER BY created_at DESC"
        rows = conn.execute(sql, params).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            for col in ("input_metadata", "tool_calls", "llm_metadata", "memory_query_ids", "latency_breakdown"):
                val = d.get(col)
                if val:
                    try:
                        d[col] = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        d[col] = None
                else:
                    d[col] = None
            results.append(d)
        return results

    def close(self) -> None:
        """Close the current thread's database connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
