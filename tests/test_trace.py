"""Tests for memory.trace — structured per-turn trace table (v2 + v3)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from memory.trace import TraceLog


@pytest.fixture()
def trace(tmp_path):
    tl = TraceLog(str(tmp_path / "test.db"))
    yield tl
    tl.close()


class TestTraceLog:
    def test_log_turn_returns_id(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="hello",
            assistant_text="hi there",
        )
        assert isinstance(rid, int)
        assert rid >= 1

    def test_log_turn_full_fields(self, trace: TraceLog):
        tool_calls = [{"name": "weather", "args": {"city": "Vancouver"}}]
        rid = trace.log_turn(
            session_id="s1",
            turn_id=2,
            user_text="what's the weather",
            assistant_text="it's sunny",
            user_emotion="neutral",
            tts_emotion="cheerful",
            path_taken="cloud",
            tool_calls=tool_calls,
            llm_model="grok-4.1-fast",
            llm_tokens_in=150,
            llm_tokens_out=80,
            latency_ms=320,
        )
        # Read back via query_for_observer which returns the core fields
        row = trace.query_for_observer(rid)
        assert row is not None
        assert row["user_text"] == "what's the weather"
        assert row["assistant_text"] == "it's sunny"
        assert row["user_emotion"] == "neutral"
        assert isinstance(row["tool_calls"], list)
        assert row["tool_calls"][0]["name"] == "weather"

        # Also verify fields not in query_for_observer via cloud traces
        traces = trace.query_cloud_traces(days=1)
        assert len(traces) == 1
        t = traces[0]
        assert t["session_id"] == "s1"
        assert t["turn_id"] == 2
        assert t["tts_emotion"] == "cheerful"
        assert t["path_taken"] == "cloud"
        assert t["llm_model"] == "grok-4.1-fast"
        assert t["llm_tokens_in"] == 150
        assert t["llm_tokens_out"] == 80
        assert t["latency_ms"] == 320

    def test_update_outcome(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="turn on lights",
            assistant_text="done",
            path_taken="local",
        )
        # Initially no outcome
        row = trace.query_for_observer(rid)
        assert row is not None

        # Set positive outcome
        trace.update_outcome(rid, signal=1)

        # Read back via direct query
        conn = trace._get_conn()
        r = conn.execute(
            "SELECT outcome_signal, outcome_at_turn_id FROM trace WHERE id = ?",
            (rid,),
        ).fetchone()
        assert r["outcome_signal"] == 1

    def test_query_for_observer(self, trace: TraceLog):
        tool_calls = [{"name": "hue", "args": {"action": "on"}}]
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="turn on lights",
            assistant_text="lights are on",
            user_emotion="happy",
            tool_calls=tool_calls,
        )
        result = trace.query_for_observer(rid)
        assert result is not None
        assert result["user_text"] == "turn on lights"
        assert result["assistant_text"] == "lights are on"
        assert result["user_emotion"] == "happy"
        assert isinstance(result["tool_calls"], list)
        assert result["tool_calls"][0]["name"] == "hue"

        # Non-existent id returns None
        assert trace.query_for_observer(9999) is None

    def test_query_for_observer_null_tool_calls(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="hi",
            assistant_text="hello",
        )
        result = trace.query_for_observer(rid)
        assert result is not None
        assert result["tool_calls"] == []

    def test_query_cloud_traces(self, trace: TraceLog):
        # Cloud path, no outcome -> included
        trace.log_turn(
            session_id="s1", turn_id=1,
            user_text="q1", assistant_text="a1",
            path_taken="cloud",
        )
        # Cloud path, positive outcome -> included
        rid2 = trace.log_turn(
            session_id="s1", turn_id=2,
            user_text="q2", assistant_text="a2",
            path_taken="cloud",
        )
        trace.update_outcome(rid2, signal=1)

        # Cloud path, negative outcome -> excluded
        rid3 = trace.log_turn(
            session_id="s1", turn_id=3,
            user_text="q3", assistant_text="a3",
            path_taken="cloud",
        )
        trace.update_outcome(rid3, signal=-1)

        # Local path -> excluded
        trace.log_turn(
            session_id="s1", turn_id=4,
            user_text="q4", assistant_text="a4",
            path_taken="local",
        )

        # Farewell path -> excluded
        trace.log_turn(
            session_id="s1", turn_id=5,
            user_text="q5", assistant_text="a5",
            path_taken="farewell",
        )

        results = trace.query_cloud_traces(days=1)
        assert len(results) == 2
        texts = {r["user_text"] for r in results}
        assert texts == {"q1", "q2"}


class TestLogTurnV3Kwargs:
    """All 29 writable kwargs round-trip through log_turn."""

    def test_all_kwargs_roundtrip(self, trace: TraceLog):
        input_meta = {"asr_text_raw": "raw", "asr_confidence": 0.9, "vad_duration_ms": 800, "audio_path": None}
        tool_calls = [{"name": "turn_on_light", "args": {"room": "living"}, "result": {"ok": True}, "ms": 42}]
        llm_meta = {"provider": "xai", "conv_id": "abc", "response_id": "xyz", "streaming": True,
                    "fallback_used": False, "truncated_by_interrupt": False, "full_response": None,
                    "cache_creation_input_tokens": None}
        mem_ids = {"observation_ids": [1, 2], "top_k_scores": [0.89, 0.76]}
        lat_bd = {"asr_ms": 120, "route_ms": 30, "memory_query_ms": 50, "direct_answer_ms": None,
                  "local_exec_ms": None, "llm_first_ms": 400, "tts_first_ms": 600, "total_ms": 900}

        rid = trace.log_turn(
            session_id="s-v3",
            turn_id=7,
            user_id="allen",
            user_text="normalized text",
            assistant_text="response",
            user_emotion="happy",
            tts_emotion="cheerful",
            input_metadata=input_meta,
            trigger_source="web_text",
            parent_trace_id=5,
            path_taken="cloud",
            intent_route_score=0.92,
            tool_calls=tool_calls,
            llm_model="grok-4.20",
            llm_tokens_in=500,
            llm_tokens_out=200,
            cache_read_input_tokens=100,
            llm_metadata=llm_meta,
            memory_query_ids=mem_ids,
            prompt_version="abcdef0123456789",
            latency_ms=900,
            ttfs_ms=600,
            latency_breakdown=lat_bd,
            end_reason="success",
            error=None,
            finish_reason="stop",
            cost_usd=0.0012,
        )
        assert isinstance(rid, int)

        # Read back all columns directly
        conn = trace._get_conn()
        row = dict(conn.execute("SELECT * FROM trace WHERE id=?", (rid,)).fetchone())

        assert row["session_id"] == "s-v3"
        assert row["turn_id"] == 7
        assert row["user_id"] == "allen"
        assert row["user_text"] == "normalized text"
        assert row["assistant_text"] == "response"
        assert row["user_emotion"] == "happy"
        assert row["tts_emotion"] == "cheerful"
        assert row["trigger_source"] == "web_text"
        assert row["parent_trace_id"] == 5
        assert row["path_taken"] == "cloud"
        assert abs(row["intent_route_score"] - 0.92) < 1e-9
        assert row["llm_model"] == "grok-4.20"
        assert row["llm_tokens_in"] == 500
        assert row["llm_tokens_out"] == 200
        assert row["cache_read_input_tokens"] == 100
        assert row["prompt_version"] == "abcdef0123456789"
        assert row["latency_ms"] == 900
        assert row["ttfs_ms"] == 600
        assert row["end_reason"] == "success"
        assert row["error"] is None
        assert row["finish_reason"] == "stop"
        assert abs(row["cost_usd"] - 0.0012) < 1e-12
        assert row["outcome_signal"] is None
        assert row["outcome_at_turn_id"] is None

        # JSON columns stored as text
        assert json.loads(row["input_metadata"]) == input_meta
        assert json.loads(row["tool_calls"]) == tool_calls
        assert json.loads(row["llm_metadata"]) == llm_meta
        assert json.loads(row["memory_query_ids"]) == mem_ids
        assert json.loads(row["latency_breakdown"]) == lat_bd

    def test_none_json_kwargs_stored_as_null(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="hi",
            assistant_text="hello",
        )
        conn = trace._get_conn()
        row = dict(conn.execute("SELECT * FROM trace WHERE id=?", (rid,)).fetchone())
        for col in ("input_metadata", "tool_calls", "llm_metadata", "memory_query_ids", "latency_breakdown"):
            assert row[col] is None, f"expected {col} NULL, got {row[col]!r}"

    def test_json_deserializes_via_query_for_debug(self, trace: TraceLog):
        tool_calls = [{"name": "search", "args": {}, "result": {}, "ms": 10}]
        input_meta = {"asr_text_raw": None, "asr_confidence": None, "vad_duration_ms": None, "audio_path": None}
        rid = trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="test",
            assistant_text="ok",
            tool_calls=tool_calls,
            input_metadata=input_meta,
        )
        results = trace.query_for_debug(session_id="s1", hours=1)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r["tool_calls"], list)
        assert r["tool_calls"][0]["name"] == "search"
        assert isinstance(r["input_metadata"], dict)
        assert r["input_metadata"]["asr_text_raw"] is None


class TestUpdateOutcomeV3:
    def test_sets_both_columns(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1", turn_id=1, user_text="ok", assistant_text="yes"
        )
        trace.update_outcome(rid, signal=1, at_turn_id=42)
        conn = trace._get_conn()
        row = conn.execute(
            "SELECT outcome_signal, outcome_at_turn_id FROM trace WHERE id=?", (rid,)
        ).fetchone()
        assert row["outcome_signal"] == 1
        assert row["outcome_at_turn_id"] == 42

    def test_none_at_turn_id_does_not_clobber(self, trace: TraceLog):
        rid = trace.log_turn(
            session_id="s1", turn_id=1, user_text="ok", assistant_text="yes"
        )
        trace.update_outcome(rid, signal=1, at_turn_id=42)
        # Second call with None should NOT overwrite outcome_at_turn_id
        trace.update_outcome(rid, signal=0, at_turn_id=None)
        conn = trace._get_conn()
        row = conn.execute(
            "SELECT outcome_signal, outcome_at_turn_id FROM trace WHERE id=?", (rid,)
        ).fetchone()
        assert row["outcome_signal"] == 0
        assert row["outcome_at_turn_id"] == 42

    def test_update_outcome_no_at_turn_id(self, trace: TraceLog):
        """Existing positional-style callers (signal only) still work."""
        rid = trace.log_turn(
            session_id="s1", turn_id=1, user_text="ok", assistant_text="yes"
        )
        trace.update_outcome(rid, signal=-1)
        conn = trace._get_conn()
        row = conn.execute("SELECT outcome_signal FROM trace WHERE id=?", (rid,)).fetchone()
        assert row["outcome_signal"] == -1


class TestCheckConstraints:
    def test_invalid_end_reason_raises(self, trace: TraceLog):
        with pytest.raises(sqlite3.IntegrityError):
            trace.log_turn(
                session_id="s1",
                turn_id=1,
                user_text="hi",
                assistant_text="hello",
                end_reason="bogus",
            )

    def test_invalid_outcome_signal_raises(self, trace: TraceLog):
        conn = trace._get_conn()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trace (session_id, turn_id, created_at, user_text, assistant_text, outcome_signal) "
                "VALUES (?, ?, datetime('now'), ?, ?, ?)",
                ("s1", 1, "hi", "hello", 99),
            )
            conn.commit()


class TestQueryForDebug:
    def _insert(self, trace: TraceLog, session_id: str, turn_id: int, user_id: str = "default_user",
                end_reason: str | None = None, error: str | None = None) -> int:
        return trace.log_turn(
            session_id=session_id,
            turn_id=turn_id,
            user_id=user_id,
            user_text=f"u{turn_id}",
            assistant_text=f"a{turn_id}",
            end_reason=end_reason,
            error=error,
        )

    def test_filter_by_session_id(self, trace: TraceLog):
        self._insert(trace, "sess-a", 1)
        self._insert(trace, "sess-a", 2)
        self._insert(trace, "sess-b", 1)
        results = trace.query_for_debug(session_id="sess-a", hours=1)
        assert len(results) == 2
        assert all(r["session_id"] == "sess-a" for r in results)

    def test_filter_by_user_id(self, trace: TraceLog):
        self._insert(trace, "s1", 1, user_id="alice")
        self._insert(trace, "s2", 2, user_id="bob")
        self._insert(trace, "s3", 3, user_id="alice")
        results = trace.query_for_debug(user_id="alice", hours=1)
        assert len(results) == 2
        assert all(r["user_id"] == "alice" for r in results)

    def test_filter_only_errors(self, trace: TraceLog):
        self._insert(trace, "s1", 1, error="something went wrong", end_reason="error")
        self._insert(trace, "s1", 2)
        self._insert(trace, "s1", 3, error="another error", end_reason="error")
        results = trace.query_for_debug(only_errors=True, hours=1)
        assert len(results) == 2
        assert all(r["error"] is not None for r in results)

    def test_filter_only_interrupted(self, trace: TraceLog):
        self._insert(trace, "s1", 1, end_reason="interrupted")
        self._insert(trace, "s1", 2, end_reason="success")
        self._insert(trace, "s1", 3, end_reason="interrupted")
        results = trace.query_for_debug(only_interrupted=True, hours=1)
        assert len(results) == 2
        assert all(r["end_reason"] == "interrupted" for r in results)

    def test_newest_first(self, trace: TraceLog):
        self._insert(trace, "s1", 1)
        self._insert(trace, "s1", 2)
        self._insert(trace, "s1", 3)
        results = trace.query_for_debug(session_id="s1", hours=1)
        turn_ids = [r["turn_id"] for r in results]
        assert turn_ids == sorted(turn_ids, reverse=True)

    def test_json_columns_deserialized(self, trace: TraceLog):
        tool_calls = [{"name": "hue", "args": {}, "result": {}, "ms": 5}]
        trace.log_turn(
            session_id="s1",
            turn_id=1,
            user_text="hi",
            assistant_text="ok",
            tool_calls=tool_calls,
            latency_breakdown={"total_ms": 300},
        )
        results = trace.query_for_debug(hours=1)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r["tool_calls"], list)
        assert r["tool_calls"][0]["name"] == "hue"
        assert isinstance(r["latency_breakdown"], dict)
        assert r["latency_breakdown"]["total_ms"] == 300
        # NULL json cols are None (not JSON strings)
        assert r["input_metadata"] is None
        assert r["llm_metadata"] is None
        assert r["memory_query_ids"] is None
