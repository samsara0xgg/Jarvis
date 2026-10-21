"""Whole-turn cases built at their moment (ADR 0317): the cut, the new expectations, the turn.

The cut runs on small databases with the shapes of the backups' (an append-only events table, text
stamps with offsets, day columns). The expectations run on a table of calls and answers. The turn
runs the real runtime at a frozen clock with a scripted model, or with a real client whose one
request goes to a local stand-in for the Messages API: nothing is paid, nothing leaves.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from jarvis.decision.llm import LLMClient
from jarvis.shared.pricing import load_pricing_table
from scripts import tool_exam_turns as turns
from scripts.tool_exam_steps import Meter, ScriptedModel, judge_steps
from scripts.tool_exam_tools import cut_at

if TYPE_CHECKING:
    from collections.abc import Iterator

AT = datetime.fromisoformat("2026-10-17T20:14:00-07:00")
HOUR_MS = 3_600_000
TABLE = load_pricing_table(Path(__file__).resolve().parents[2] / "data" / "pricing.json")


def test_the_cut_removes_what_was_made_at_or_after_the_moment_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """Rows by when they were made, in any offset; days only without a stamp; triggers come back."""
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
        "CREATE TABLE doing_answers (id TEXT, day TEXT, start_ms INTEGER, end_ms INTEGER, ts TEXT);"
        "CREATE TABLE day_marks (day TEXT);"
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
        [("2026-10-16", "2026-10-17T05:00:00-07:00"), ("2026-10-17", "2026-10-18T04:00:00-07:00")],
    )
    db.executemany(  # stretches of the moment's day: one written before it, one after
        "INSERT INTO doing_answers VALUES (?, ?, ?, ?, ?)",
        [
            ("morning", "2026-10-17", ms - 9 * HOUR_MS, ms - 8 * HOUR_MS, "2026-10-17T13:00-07:00"),
            ("evening", "2026-10-17", ms - HOUR_MS, ms + HOUR_MS, "2026-10-17T23:00-07:00"),
        ],
    )
    db.executemany("INSERT INTO day_marks VALUES (?)", [("2026-10-16",), ("2026-10-17",)])
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
    assert [r[0] for r in db.execute("SELECT id FROM doing_answers")] == ["morning"]
    assert [r[0] for r in db.execute("SELECT day FROM day_marks")] == ["2026-10-16"]
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


def _brain_settings(directory: Path) -> Path:
    """A brain's settings file with nothing of his in it."""
    path = directory / "settings.brain.yaml"
    path.write_text("runtime:\n  role: brain\n", encoding="utf-8")
    return path


def test_a_built_case_runs_at_the_frozen_clock_and_stubs_a_deep_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No logged request: the runtime builds the prompt and state block; deep search is not run."""
    # web_search is on the menu only with a search key; the deep call is stubbed, so none is sent.
    monkeypatch.setenv("TAVILY_API_KEY", "exam-placeholder-key")
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
    line = turns.run_one(
        case, ScriptedModel(script), "fake", 0, tmp_path, TABLE, Meter(None, TABLE), None,
        _brain_settings(tmp_path),
    )
    assert line["error"] is None, line["error"]
    assert line["text"] == "这个我暂时深查不了。"
    call = line["steps"][0]["calls"][0]
    assert call["name"] == "web_search"
    assert "nested paid request" in call["result"]
    assert "Time: 2026-10-17T20:14-07:00" in line["built"]["state_block"]


def test_a_built_case_without_brain_settings_names_the_missing_file(tmp_path: Path) -> None:
    """The settings are his brain's; a run without them stops and says where it looked."""
    case = {"id": "no-settings", "said": "x", "at": "2026-10-17T20:14:00-07:00", "zone": "UTC"}
    missing = tmp_path / "settings.brain.yaml"
    line = turns.run_one(
        case, ScriptedModel([{"text": "x"}]), "fake", 0, tmp_path, TABLE, Meter(None, TABLE),
        None, missing,
    )
    assert line["error"] == f"LookupError: no brain settings at {missing}; pass --brain-settings"


class _Messages(BaseHTTPRequestHandler):
    """A stand-in for the Messages API: every request gets one short text answer."""

    bodies: ClassVar[list[dict[str, Any]]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        body = json.dumps({
            "id": "msg_exam", "type": "message", "role": "assistant",
            "model": "claude-haiku-5-5", "content": [{"type": "text", "text": "Nothing yet."}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 4},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """Silence the request log."""


@pytest.fixture
def messages_api() -> Iterator[str]:
    """A local Messages API and its base URL."""
    _Messages.bodies = []
    server = HTTPServer(("127.0.0.1", 0), _Messages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_a_real_client_answers_a_built_case_through_the_closed_network(
    messages_api: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client's own request goes out and is metered; the turn's patch does not loop on it."""
    monkeypatch.setenv("EXAM_MESSAGES_KEY", "exam-placeholder-key")
    client = LLMClient({
        "provider": "anthropic",
        "presets": {"haiku": {
            "provider": "anthropic", "model": "claude-haiku-5-5", "base_url": messages_api,
            "api_key_env": "EXAM_MESSAGES_KEY", "max_tokens": 64,
        }},
        "default_preset": "haiku",
        "max_retries": 0,
    })
    case = {"id": "real-client", "said": "anything on tonight", "at": AT.isoformat(), "zone": "UTC"}
    meter = Meter(0.05, TABLE)
    line = turns.run_one(
        case, client, "haiku", 0, tmp_path, TABLE, meter, None, _brain_settings(tmp_path),
    )
    assert line["error"] is None, line["error"]
    assert line["text"] == "Nothing yet."
    assert _Messages.bodies, "the client's request never reached the API"
    assert _Messages.bodies[0]["model"] == "claude-haiku-5-5"
    assert meter.calls == len(_Messages.bodies)
    assert meter.spent > 0
