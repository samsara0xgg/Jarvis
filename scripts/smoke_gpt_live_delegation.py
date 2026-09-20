"""Hermetic smoke for the ADR-0016 delegation wiring (Commit 3 of phase B).

Drives the real composition-root pieces against a temporary Event Log and
memory.db, no daemon, no network, and asserts on the durable observables:

- the D21 inbox: a redelivered delegation appends ONE ``surface.user_intent``
  row carrying ``channel=gpt_live``, ``source_surface=gpt_live`` and the
  surface's ``record_id``, and replays the same ``turn_id``;
- ``lookup_result``: nothing while the turn runs, ignores the ``commentary``
  phase emission, returns the ``final`` answer with ``voice_text``, and a
  correlated ``turn.failed`` as a failure;
- ``brief_note``: profile first, newest records chosen backwards, emitted in
  time order, within the character budget;
- ``ReadOnlyToolRegistry``: only ``read_only`` tools, and a mutating dispatch
  raises ``UnknownToolError`` before any ``action.dispatched`` row exists.

Run: ``.venv/bin/python scripts/smoke_gpt_live_delegation.py``
"""

from __future__ import annotations

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from jarvis.execution.tools import ReadOnlyToolRegistry, UnknownToolError, build_default_registry
from jarvis.runtime.inherent_loop import _LiveBackend
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.memory_db import MemorySettings, append_record, brief_note, open_memory_db

if TYPE_CHECKING:
    import sqlite3

CHECKS = 0
REQUEST = "帮我查明天多伦多天气"
REQUEST_ROW = "live:S1:allen:1000"
BRIEF_BUDGET = 1500


def _out(line: str) -> None:
    """Print one operator-facing line; stdout is this script's interface."""
    sys.stdout.write(line + "\n")


def _ok(cond: bool, what: str) -> None:  # noqa: FBT001 - assertion helper
    """Count one check; the first failure ends the run with exit code 1."""
    global CHECKS  # noqa: PLW0603 - smoke counter
    CHECKS += 1
    if not cond:
        _out(f"FAIL {what}")
        sys.exit(1)
    _out(f"ok   {what}")


def _count(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    """Scalar COUNT(*) helper."""
    return int(conn.execute(sql, params).fetchone()[0])


def _check_inbox_and_lookup(backend: _LiveBackend, event_log: Path) -> None:
    """D2 (one row per delegation, replay) and D4/D5 (lookup_result states)."""
    backend.record("allen", REQUEST, REQUEST_ROW)
    turn_a = backend.delegate(REQUEST, "item_d1", "S1", REQUEST_ROW)
    turn_b = backend.delegate(REQUEST, "item_d1", "S1", REQUEST_ROW)
    _ok(turn_a == turn_b, f"redelivery replays the same turn_id {turn_a}")
    with closing(open_event_log(event_log)) as conn:
        rows = _count(conn, "SELECT COUNT(*) FROM events WHERE type = 'surface.user_intent'")
        _ok(rows == 1, "one surface.user_intent row after two deliveries")
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM events WHERE type = 'surface.user_intent'",
            ).fetchone()[0],
        )
        _ok(payload["channel"] == "gpt_live", "payload channel is gpt_live")
        _ok(payload["source_surface"] == "gpt_live", "payload source_surface is gpt_live")
        _ok(payload["record_id"] == REQUEST_ROW, "payload carries the surface record_id")
        other = backend.delegate("另一个问题", "item_d2", "S1", "live:S1:allen:2000")
        _ok(other != turn_a, "a new delegation id mints a new turn")

        _ok(backend.lookup_result(turn_a) is None, "no result while the turn runs")
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={"turn_id": turn_a, "text": "我在查", "phase": "commentary"},
            correlation={"turn_id": turn_a},
        )
        _ok(backend.lookup_result(turn_a) is None, "commentary-phase emission is not the answer")
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": turn_a,
                "text": "明天多伦多多云, 最高二十度, 晚间转晴。详细逐小时见文档。",
                "voice_text": "明天多伦多多云, 最高二十度。",
                "phase": "final",
            },
            correlation={"turn_id": turn_a},
        )
        result = backend.lookup_result(turn_a)
        _ok(result is not None and result.status == "answered", "final emission -> answered")
        _ok(
            result is not None and result.voice_text == "明天多伦多多云, 最高二十度。",
            "voice_text carried",
        )
        emit_event(
            conn,
            type="turn.failed",
            payload={"turn_id": other, "exception_repr": "RuntimeError('boom')"},
            correlation={"turn_id": other},
        )
        failed = backend.lookup_result(other)
        _ok(
            failed is not None and failed.status == "failed" and "boom" in str(failed.reason),
            "correlated turn.failed -> failed with reason",
        )


def _check_memory(memory: MemorySettings) -> None:
    """D3 (request row written once) and D7 (budgeted, ordered brief)."""
    with closing(open_memory_db(memory.db_path)) as mem:
        _ok(
            _count(mem, "SELECT COUNT(*) FROM records WHERE id = ?", REQUEST_ROW) == 1,
            "request row written once by the surface",
        )
        mem.execute(
            "INSERT INTO profile (id, ts, text) VALUES (?, ?, ?)",
            ("p1", "2026-09-12T00:00:00-04:00", "住多伦多"),
        )
        mem.commit()
    for i in range(60):
        append_record(
            memory.db_path, record_id=f"r{i:03d}", source="allen", text=f"第{i:03d}句" + "话" * 40,
        )
    brief = brief_note(memory.db_path, max_chars=BRIEF_BUDGET)
    _ok(len(brief) <= BRIEF_BUDGET, f"brief within budget ({len(brief)} chars)")
    _ok(brief.startswith("[关于 Allen]\n- 住多伦多"), "brief leads with the profile")
    _ok("search_records" not in brief, "brief has no tool hint")
    _ok("第059句" in brief and "第000句" not in brief, "brief keeps the newest records")
    lines = [ln for ln in brief.splitlines() if ln.startswith("[2026")]
    _ok(lines == sorted(lines), "brief records are in time order")


def _check_registry_view(memory: MemorySettings, event_log: Path) -> None:
    """D6: the read-only view hides mutating tools and refuses their dispatch."""
    full = build_default_registry(memory_db_path=memory.db_path)
    view = ReadOnlyToolRegistry(full)
    names = {t.name for t in view.get_definitions()}
    _ok(all(t.read_only for t in view.get_definitions()), "view exposes only read_only tools")
    _ok({"write_file", "create_memo"}.isdisjoint(names), "view hides mutating tools")
    _ok({"web_search", "web_fetch", "search_records"} <= names, f"view keeps {sorted(names)}")
    _ok("write_file" in {t.name for t in full.get_definitions()}, "shared registry untouched")
    request = SimpleNamespace(tool_name="write_file", action_id="A1")
    refused = False
    try:
        view.dispatch(request, None, None, None)  # type: ignore[arg-type]
    except UnknownToolError:
        refused = True
    _ok(refused, "mutating dispatch raises UnknownToolError")
    with closing(open_event_log(event_log)) as conn:
        _ok(
            _count(conn, "SELECT COUNT(*) FROM events WHERE type = 'action.dispatched'") == 0,
            "no action.dispatched row exists",
        )


def main() -> None:
    """Run every check against a throwaway Event Log and memory.db."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        event_log = root / "events.db"
        memory = MemorySettings(db_path=root / "memory.db", audio_dir=root / "audio")
        backend = _LiveBackend(event_log_path=event_log, memory=memory)
        _check_inbox_and_lookup(backend, event_log)
        _check_memory(memory)
        _check_registry_view(memory, event_log)
    _out(f"SMOKE OK {CHECKS}/{CHECKS}")


if __name__ == "__main__":
    main()
