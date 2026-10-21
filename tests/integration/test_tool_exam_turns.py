"""Whole-turn cases built at their moment (ADR 0317): the cut, the new expectations, the turn.

The cut runs on small databases with the shapes of the backups' (an append-only events table, text
stamps with offsets, day columns). The expectations run on a table of calls and answers. The turn
runs the real runtime at a frozen clock with a scripted model: nothing is paid, nothing leaves.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jarvis.shared.pricing import load_pricing_table
from scripts import tool_exam_turns as turns
from scripts.tool_exam_steps import Meter, ScriptedModel, judge_steps
from scripts.tool_exam_tools import cut_at

AT = datetime.fromisoformat("2026-10-17T20:14:00-07:00")


def test_the_cut_removes_what_was_made_at_or_after_the_moment_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """Events by epoch, stamps in any offset, days from the moment's day; triggers come back."""
    ms = int(AT.timestamp() * 1000)
    events = tmp_path / "mac_events.db"
    db = sqlite3.connect(events)
    db.executescript(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, ts_epoch_ms INTEGER);"
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events"
        " BEGIN SELECT RAISE(ABORT, 'append-only'); END;"
        "CREATE TABLE event_log_metadata (log_epoch TEXT);"
        "INSERT INTO event_log_metadata VALUES ('e1');"
    )
    db.executemany("INSERT INTO events (ts_epoch_ms) VALUES (?)", [(ms - 1,), (ms,), (ms + 1,)])
    db.commit()
    db.close()
    memory = tmp_path / "memory.db"
    db = sqlite3.connect(memory)
    db.executescript(
        "CREATE TABLE records (id TEXT, ts TEXT);"
        "CREATE TABLE day_prose (day TEXT, ts TEXT);"
        "CREATE TABLE job_mail (id TEXT, received_at TEXT, event_at TEXT);"
        "CREATE TABLE places (id TEXT, created_at TEXT, updated_at TEXT);"
        "CREATE TABLE sent (id TEXT);"
        "INSERT INTO sent VALUES ('s');"
    )
    db.executemany(
        "INSERT INTO records VALUES (?, ?)",
        [
            ("before", "2026-10-17T20:13:59-07:00"),
            ("at", "2026-10-17T20:14:00-07:00"),
            ("utc-after", "2026-10-18T03:30:00+00:00"),
            ("utc-before", "2026-10-18T03:00:00+00:00"),
        ],
    )
    db.executemany(
        "INSERT INTO day_prose VALUES (?, ?)",
        [("2026-10-16", "2026-10-17T05:00:00-07:00"), ("2026-10-17", "2026-10-17T05:00:00-07:00")],
    )
    db.executemany(
        "INSERT INTO job_mail VALUES (?, ?, ?)",
        [("old", "2026-10-17T20:00:00+00:00", "2026-10-30T10:00:00-07:00")],
    )
    db.executemany(
        "INSERT INTO places VALUES (?, ?, ?)",
        [("home", "2026-10-11T13:25:30-07:00", "2026-10-18T09:00:00-07:00")],
    )
    db.commit()
    db.close()

    left = [*cut_at(events, AT, "America/Vancouver"), *cut_at(memory, AT, "America/Vancouver")]

    db = sqlite3.connect(events)
    assert [r[0] for r in db.execute("SELECT ts_epoch_ms FROM events")] == [ms - 1]
    assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")] == [
        "events_no_delete"
    ]
    db.close()
    db = sqlite3.connect(memory)
    assert sorted(r[0] for r in db.execute("SELECT id FROM records")) == ["before", "utc-before"]
    assert [r[0] for r in db.execute("SELECT day FROM day_prose")] == ["2026-10-16"]
    # a row edited later, and a time that lies ahead, are not rows made after the cut
    assert db.execute("SELECT count(*) FROM job_mail").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM places").fetchone()[0] == 1
    db.close()
    assert sorted(left) == ["event_log_metadata", "sent"]


def _line(
    calls: list[tuple[str, Any]], text: str = "", *, cards: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    steps = [{"calls": [{"name": n, "args": a} for n, a in calls]}]
    return {"steps": steps, "text": text, "confirmations": cards or [], "error": None}


EXPECTATIONS = [
    (
        {"args_must": {"keep": [{"when": "re:1 ?pm|13:00"}, {"when": "never"}]}},
        _line([("keep", {"what": "练车", "when": "tomorrow 1pm"})]),
        True,
    ),
    (
        {"args_must": {"keep": [{"when": "re:1 ?pm"}]}},
        _line([("keep", {"what": "练车", "when": "tomorrow noon"})]),
        False,
    ),
    (
        {"args_must": {"keep": [{"what": "练车", "kind": "go"}]}},
        _line([("keep", {"what": "练车"})]),
        False,
    ),
    (
        {"args_must_not": {"keep": [{"kind": "go"}]}},
        _line([("keep", {"what": "x", "kind": "go", "when": "now"})]),
        False,
    ),
    ({"args_must_not": {"keep": [{"kind": "go"}]}}, _line([("keep", {"what": "x"})]), True),
    ({"min_calls": {"bus": 2}}, _line([("bus", {})]), False),
    ({"min_calls": {"bus": 2}}, _line([("bus", {}), ("bus", {"to": "home"})]), True),
    ({"no_card": True}, _line([("keep", {})], cards=[{"tool": "keep"}]), False),
    ({"no_card": True}, _line([("keep", {})]), True),
    ({"claims_need_call": {"keep": ["记好了", "set a reminder"]}}, _line([], "记好了"), False),
    (
        {"claims_need_call": {"keep": ["记好了"]}},
        _line([("keep", {})], "记好了"),
        True,
    ),
    ({"claims_need_call": {"keep": ["记好了"]}}, _line([], "今天晴"), True),
]


@pytest.mark.parametrize(("spec", "line", "passes"), EXPECTATIONS)
def test_the_new_step_expectations_judge_calls_cards_and_claims(
    spec: dict[str, Any], line: dict[str, Any], *, passes: bool
) -> None:
    """Each field of ``expect.steps`` added for built cases passes and fails on a run it names."""
    ok, label = judge_steps(spec, line, None)
    assert ok is passes, label


def test_a_built_case_runs_at_the_frozen_clock_and_stubs_a_deep_search(tmp_path: Path) -> None:
    """No logged request: the runtime builds the prompt and state block; deep search is not run."""
    case = {
        "id": "built-clock",
        "said": "查一下这个",
        "at": "2026-10-17T20:14:00-07:00",
        "zone": "America/Vancouver",
    }
    script: list[dict[str, Any]] = [
        {"tool_calls": [{"name": "web_search", "arguments": {"query": "x", "depth": "deep"}}]},
        {"text": "这个我暂时深查不了。"},
    ]
    table = load_pricing_table(Path(__file__).resolve().parents[2] / "data" / "pricing.json")
    line = turns.run_one(
        case, ScriptedModel(script), "fake", 0, tmp_path, table, Meter(None, table), None
    )
    assert line["error"] is None, line["error"]
    assert line["text"] == "这个我暂时深查不了。"
    call = line["steps"][0]["calls"][0]
    assert call["name"] == "web_search"
    assert "nested paid request" in call["result"]
    assert "Time: 2026-10-17T20:14-07:00" in line["built"]["state_block"]
