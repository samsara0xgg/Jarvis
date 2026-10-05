"""open_path's frequency ranking reads only `action.result_observed` rows, with the old result."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jarvis.execution import path_resolver
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def _observed(conn: sqlite3.Connection, action_id: str, tool_output: str | None) -> None:
    emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": action_id, "semantics": "completed", "tool_output": tool_output},
    )


def _old_whole_log_count(conn: sqlite3.Connection) -> dict[str, int]:
    """The pre-change implementation, kept here as the reference."""
    counts: dict[str, int] = {}
    for evt in iter_events(conn):
        if evt.type != "action.result_observed":
            continue
        raw = evt.payload.get("tool_output")
        if not isinstance(raw, str):
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("opened_path"), str):
            counts[parsed["opened_path"]] = counts.get(parsed["opened_path"], 0) + 1
    return counts


def _log(tmp_path: Path) -> sqlite3.Connection:
    conn = open_event_log(tmp_path / "events.db")
    for i, path in enumerate(["/a/x.txt", "/a/x.txt", "/b/y.txt", "/a/x.txt"]):
        _observed(conn, f"A{i}", json.dumps({"opened_path": path}))
    _observed(conn, "Abad", "not json")
    _observed(conn, "Alist", json.dumps(["/a/x.txt"]))
    _observed(conn, "Anone", None)
    _observed(conn, "Anum", json.dumps({"opened_path": 3}))
    emit_event(conn, type="turn.started", payload={"turn_id": "T1"})
    return conn


def test_frequencies_match_the_whole_log_count(tmp_path: Path) -> None:
    """Same counts as the pre-change whole-log pass, junk rows skipped."""
    conn = _log(tmp_path)
    counts = path_resolver._open_frequencies(conn)  # noqa: SLF001
    assert counts == {"/a/x.txt": 3, "/b/y.txt": 1}
    assert counts == _old_whole_log_count(conn)


def test_frequencies_never_walk_the_whole_log(tmp_path: Path) -> None:
    """Every SELECT over events carries a type predicate."""
    conn = _log(tmp_path)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    path_resolver._open_frequencies(conn)  # noqa: SLF001
    selects = [sql for sql in seen if "FROM events" in sql]
    assert selects
    assert all("type IN" in sql for sql in selects)


def test_a_closed_connection_degrades_to_no_frequencies(tmp_path: Path) -> None:
    """A read failure is a ranking miss, never an error."""
    conn = _log(tmp_path)
    conn.close()
    assert path_resolver._open_frequencies(conn) == {}  # noqa: SLF001


def test_rank_prefers_the_more_opened_path(tmp_path: Path) -> None:
    """Frequency leads the rank tuple."""
    candidates = [tmp_path / "x.txt", tmp_path / "y.txt"]
    freq = {str(candidates[0]): 1, str(candidates[1]): 5}
    assert path_resolver._rank(  # noqa: SLF001
        candidates, mandatory_ascii=["x"], freq_by_path=freq,
    ) == candidates[1]
