"""Tests for memory.trace — structured per-turn trace table for v2."""
from __future__ import annotations

import json

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
