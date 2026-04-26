"""Tests for ``core/mcp_server.py``.

In-process tests use ``FastMCP.call_tool`` directly (no subprocess).
Read-only enforcement, malformed-JSON handling, filter logic, and bounds
validation are all exercised against a temporary SQLite trace DB.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from core import mcp_server
from memory.trace import TraceLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_rows(tlog: TraceLog) -> dict[str, int]:
    """Seed a deterministic set of rows; return ids keyed by tag."""
    ids: dict[str, int] = {}

    ids["plain"] = tlog.log_turn(
        session_id="sess-A",
        turn_id=1,
        user_text="hello",
        assistant_text="hi",
        tool_calls=[{"name": "get_weather", "args": {"city": "Toronto"}, "ms": 250}],
        llm_metadata={"finish_reason": "stop", "model": "grok-4.20"},
    )
    ids["errored"] = tlog.log_turn(
        session_id="sess-A",
        turn_id=2,
        user_text="break",
        assistant_text="",
        error="RuntimeError: deliberate failure",
    )
    ids["interrupted"] = tlog.log_turn(
        session_id="sess-A",
        turn_id=3,
        user_text="long story",
        assistant_text="(cut)",
        end_reason="interrupted",
    )
    ids["other_session"] = tlog.log_turn(
        session_id="sess-B",
        turn_id=1,
        user_text="from B",
        assistant_text="hi from B",
    )
    return ids


def _backdate_row(db_path: Path, trace_id: int, hours_ago: int) -> None:
    """Move a row's ``created_at`` backwards in time."""
    ts = (datetime.now() - timedelta(hours=hours_ago)).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE trace SET created_at = ? WHERE id = ?", (ts, trace_id))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path) -> tuple[Path, dict[str, int]]:
    """Build a fresh trace DB at ``tmp_path/trace.db`` with seeded rows."""
    db_path = tmp_path / "trace.db"
    tlog = TraceLog(db_path)
    ids = _seed_rows(tlog)
    tlog.close()
    return db_path, ids


# ---------------------------------------------------------------------------
# resolve_db_path
# ---------------------------------------------------------------------------


def test_resolve_db_path_cli_override_wins(tmp_path: Path) -> None:
    custom = tmp_path / "custom.db"
    custom.touch()
    assert mcp_server.resolve_db_path(str(custom)) == custom.resolve()


def test_resolve_db_path_default_when_no_override(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "fakeroot"
    fake_root.mkdir()
    monkeypatch.setattr(mcp_server, "_project_root", lambda: fake_root)
    expected = (fake_root / mcp_server._DEFAULT_DB_RELATIVE).resolve()
    assert mcp_server.resolve_db_path(None) == expected


def test_resolve_db_path_reads_config_yaml(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "fakeroot"
    fake_root.mkdir()
    (fake_root / "config.yaml").write_text(
        "memory:\n  db_path: data/custom_memory.db\n", encoding="utf-8"
    )
    monkeypatch.setattr(mcp_server, "_project_root", lambda: fake_root)
    assert mcp_server.resolve_db_path(None) == (
        fake_root / "data/custom_memory.db"
    ).resolve()


def test_resolve_db_path_handles_malformed_config(monkeypatch, tmp_path: Path) -> None:
    fake_root = tmp_path / "fakeroot"
    fake_root.mkdir()
    (fake_root / "config.yaml").write_text(": not valid yaml :", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_project_root", lambda: fake_root)
    # Should fall back to default, not raise
    assert mcp_server.resolve_db_path(None) == (
        fake_root / mcp_server._DEFAULT_DB_RELATIVE
    ).resolve()


# ---------------------------------------------------------------------------
# _connect_ro
# ---------------------------------------------------------------------------


def test_connect_ro_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mcp_server._connect_ro(tmp_path / "nope.db")


def test_connect_ro_blocks_writes(seeded_db: tuple[Path, dict[str, int]]) -> None:
    db_path, _ = seeded_db
    conn = mcp_server._connect_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE trace SET user_text = 'hacked' WHERE id = 1")
            conn.commit()
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM trace")
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _deserialize_row
# ---------------------------------------------------------------------------


def test_deserialize_row_parses_json_columns() -> None:
    row = {
        "id": 1,
        "tool_calls": json.dumps([{"name": "x", "ms": 100}]),
        "llm_metadata": json.dumps({"k": "v"}),
        "input_metadata": None,
        "latency_breakdown": "",
        "cited_obs_ids": json.dumps([7, 8]),
        "user_text": "hi",
    }
    out = mcp_server._deserialize_row(row)
    assert out["tool_calls"] == [{"name": "x", "ms": 100}]
    assert out["llm_metadata"] == {"k": "v"}
    assert out["input_metadata"] is None
    assert out["latency_breakdown"] is None
    assert out["cited_obs_ids"] == [7, 8]
    assert out["user_text"] == "hi"  # non-JSON column passes through


def test_deserialize_row_wraps_malformed_json() -> None:
    row = {"id": 5, "tool_calls": "{not json", "input_metadata": None,
           "llm_metadata": None, "latency_breakdown": None, "cited_obs_ids": None}
    out = mcp_server._deserialize_row(row)
    assert isinstance(out["tool_calls"], dict)
    assert out["tool_calls"]["_error"] == "JSONDecodeError"
    assert out["tool_calls"]["_raw"] == "{not json"


# ---------------------------------------------------------------------------
# build_server: tool registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_server_registers_three_tools(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "trace_get_by_id",
        "trace_get_by_session_turn",
        "trace_query_recent",
    ]


# ---------------------------------------------------------------------------
# trace_query_recent
# ---------------------------------------------------------------------------


def _result_rows(call_result) -> list[dict]:
    """Unwrap FastMCP structured-content response to the list payload."""
    if isinstance(call_result, dict):
        return call_result.get("result", [])
    if isinstance(call_result, tuple):
        # FastMCP returns (content_blocks, structured) as of recent versions
        for part in call_result:
            if isinstance(part, dict) and "result" in part:
                return part["result"]
    raise AssertionError(f"unexpected call result shape: {type(call_result)}")


@pytest.mark.asyncio
async def test_query_recent_returns_all_seeded_rows(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 24})
    rows = _result_rows(result)
    returned_ids = {r["id"] for r in rows}
    assert returned_ids == set(ids.values())


@pytest.mark.asyncio
async def test_query_recent_orders_newest_first(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 24})
    rows = _result_rows(result)
    timestamps = [r["created_at"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_query_recent_only_errors_filter(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool(
        "trace_query_recent", {"hours": 24, "only_errors": True}
    )
    rows = _result_rows(result)
    assert len(rows) == 1
    assert rows[0]["id"] == ids["errored"]


@pytest.mark.asyncio
async def test_query_recent_only_interrupted_filter(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool(
        "trace_query_recent", {"hours": 24, "only_interrupted": True}
    )
    rows = _result_rows(result)
    assert len(rows) == 1
    assert rows[0]["id"] == ids["interrupted"]


@pytest.mark.asyncio
async def test_query_recent_respects_limit(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 24, "limit": 2})
    rows = _result_rows(result)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_query_recent_clamps_oversized_limit(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    # limit=10000 should silently clamp to _MAX_LIMIT (200), but seeded rows
    # are only 4 — assert it returns all without raising.
    result = await server.call_tool("trace_query_recent", {"hours": 24, "limit": 10000})
    rows = _result_rows(result)
    assert len(rows) == 4


@pytest.mark.asyncio
async def test_query_recent_excludes_old_rows(seeded_db) -> None:
    db_path, ids = seeded_db
    _backdate_row(db_path, ids["plain"], hours_ago=48)
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 1})
    rows = _result_rows(result)
    returned_ids = {r["id"] for r in rows}
    assert ids["plain"] not in returned_ids


@pytest.mark.asyncio
async def test_query_recent_includes_fresh_rows_within_window(seeded_db) -> None:
    """Regression: with hours=1 a row written 'now' in Python isoformat must
    appear. The original SQL ``created_at > datetime('now', '-1 hours')`` was a
    string-lex compare between local-tz isoformat-with-T and UTC space-format,
    silently excluding everything. Fixed with ``julianday(...) > julianday(...,
    'localtime')``.
    """
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 1})
    rows = _result_rows(result)
    returned_ids = {r["id"] for r in rows}
    # All seeded rows were just inserted (no backdating), every id must show up
    for key, rid in ids.items():
        assert rid in returned_ids, f"fresh row {key} (id={rid}) missing from 1h window"


@pytest.mark.asyncio
async def test_query_recent_rejects_invalid_hours(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    with pytest.raises(Exception):
        await server.call_tool("trace_query_recent", {"hours": 0})
    with pytest.raises(Exception):
        await server.call_tool("trace_query_recent", {"hours": 99999})


@pytest.mark.asyncio
async def test_query_recent_deserializes_json_columns(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_query_recent", {"hours": 24})
    rows = _result_rows(result)
    plain = next(r for r in rows if r["id"] == ids["plain"])
    assert isinstance(plain["tool_calls"], list)
    assert plain["tool_calls"][0]["name"] == "get_weather"
    assert isinstance(plain["llm_metadata"], dict)
    assert plain["llm_metadata"]["finish_reason"] == "stop"


# ---------------------------------------------------------------------------
# trace_get_by_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_id_returns_row(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_get_by_id", {"trace_id": ids["plain"]})
    if isinstance(result, dict):
        row = result.get("result")
    else:
        row = next((p["result"] for p in result if isinstance(p, dict) and "result" in p), None)
    assert row is not None
    assert row["id"] == ids["plain"]
    assert row["user_text"] == "hello"


@pytest.mark.asyncio
async def test_get_by_id_missing_returns_null(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool("trace_get_by_id", {"trace_id": 999_999})
    if isinstance(result, dict):
        row = result.get("result")
    else:
        row = next((p["result"] for p in result if isinstance(p, dict) and "result" in p), None)
    assert row is None


# ---------------------------------------------------------------------------
# trace_get_by_session_turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_session_turn_returns_row(seeded_db) -> None:
    db_path, ids = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool(
        "trace_get_by_session_turn",
        {"session_id": "sess-A", "turn_id": 1},
    )
    if isinstance(result, dict):
        row = result.get("result")
    else:
        row = next((p["result"] for p in result if isinstance(p, dict) and "result" in p), None)
    assert row is not None
    assert row["id"] == ids["plain"]


@pytest.mark.asyncio
async def test_get_by_session_turn_missing_returns_null(seeded_db) -> None:
    db_path, _ = seeded_db
    server = mcp_server.build_server(db_path)
    result = await server.call_tool(
        "trace_get_by_session_turn",
        {"session_id": "nope", "turn_id": 999},
    )
    if isinstance(result, dict):
        row = result.get("result")
    else:
        row = next((p["result"] for p in result if isinstance(p, dict) and "result" in p), None)
    assert row is None
