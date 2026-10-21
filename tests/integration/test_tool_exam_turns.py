"""Whole-turn cases built at their moment (ADR 0317): the cut, the new expectations, the turn.

The cut runs on small databases with the shapes of the backups' (an append-only events table, text
stamps with offsets, day columns). The expectations run on a table of calls and answers. The turn
runs the real runtime at a frozen clock with a scripted model, or with a real client whose one
request goes to a local stand-in for the Messages API: nothing is paid, nothing leaves.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import pytest
import yaml

from jarvis.decision.llm import LLMClient
from jarvis.runtime.weatherkit import ENDPOINT, NOT_SET_UP
from jarvis.shared.pricing import load_pricing_table
from scripts import tool_exam_steps as steps
from scripts import tool_exam_turns as turns
from scripts.tool_exam_steps import Meter, classify, judge_steps
from scripts.tool_exam_tools import Item, Recording, _Transport, cut_at, sandbox
from tests.support.model import Call, Reply, ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Iterator

AT = datetime.fromisoformat("2026-10-17T20:14:00-07:00")
HOUR_MS = 3_600_000
REPO = Path(__file__).resolve().parents[2]
TABLE = load_pricing_table(REPO / "data" / "pricing.json")
_KEY_NAME = re.compile(r"\b[A-Z][A-Z0-9_]*_API_KEY\b")


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
    model = ScriptedModel(
        Reply(calls=(Call("web_search", {"query": "x", "depth": "deep"}),)),
        Reply(text="这个我暂时深查不了。"),
    )
    line = turns.run_one(
        case, model, "fake", 0, tmp_path, TABLE, Meter(None, TABLE), None,
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
        case, ScriptedModel(Reply(text="x")), "fake", 0, tmp_path, TABLE, Meter(None, TABLE),
        None, missing,
    )
    assert line["error"] == f"LookupError: no brain settings at {missing}; pass --brain-settings"


def test_a_built_case_has_the_weather_on_though_his_key_file_is_out_of_reach(
    tmp_path: Path,
) -> None:
    """His settings name his WeatherKit key; the replayed turn signs with a throwaway one."""
    settings = tmp_path / "settings.brain.yaml"
    settings.write_text(
        "runtime:\n  role: brain\n"
        "home:\n  weather:\n    latitude: 37.7749\n    longitude: -122.4194\n"
        "weather:\n  weatherkit:\n    key_id: KEY0000000\n    service_id: his.service\n"
        "    key_file: ~/.jarvis/weatherkit/AuthKey.p8\n",
        encoding="utf-8",
    )
    case = {"id": "built-weather", "said": "今天会下雨吗", "at": AT.isoformat(), "zone": "UTC"}
    model = ScriptedModel(Reply(calls=(Call("weather", {}),)), Reply(text="天气暂时查不到。"))
    line = turns.run_one(
        case, model, "fake", 0, tmp_path, TABLE, Meter(None, TABLE), None, settings,
    )
    assert line["error"] is None, line["error"]
    assert NOT_SET_UP not in line["steps"][0]["calls"][0]["result"]
    assert any(ENDPOINT in miss for miss in line["built"]["stubbed_providers"])


_HUE_LISTING = {
    "mcp__hue__list_lights": {
        "description": "List every light on the bridge.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "mcp__hue__set_light": {
        "description": "Update one light.",
        "input_schema": {
            "type": "object",
            "properties": {"light_id": {"type": "string"}, "on": {"type": "boolean"}},
            "required": ["light_id"],
        },
    },
}


def test_only_the_plugins_and_servers_whose_tools_are_listed_stay_in_a_built_case(
    tmp_path: Path,
) -> None:
    """A listed server keeps the brain's entry; every other plugin and server is dropped."""
    source = tmp_path / "settings.brain.yaml"
    source.write_text(
        "tools:\n  plugins:\n    hue: {enabled: true}\n    notion: {enabled: true}\n"
        "  mcp:\n    always_loaded: [mcp__hue__set_light]\n"
        "    servers:\n      linear: {url: https://mcp.linear.invalid}\n",
        encoding="utf-8",
    )
    turns.brain_settings(tmp_path, source, {"hue"})
    kept = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
    assert kept["tools"]["plugins"] == {"hue": {"enabled": True}}
    assert kept["tools"]["mcp"] == {"always_loaded": ["mcp__hue__set_light"], "servers": {}}
    assert turns.placeholders(tmp_path, {"hue"}) == {
        "HUE_BRIDGE_HOST": "exam-placeholder-key",
        "HUE_APPLICATION_KEY": "exam-placeholder-key",
    }


def test_a_listed_plugin_is_offered_as_the_brain_offers_it_and_answers_from_the_recording(
    tmp_path: Path,
) -> None:
    """Hue listed beside the cases: its read answers from the recording, a change asks first."""
    (tmp_path / "mcp-tools.json").write_text(json.dumps(_HUE_LISTING), encoding="utf-8")
    lights = {"lights": [{"id": "lamp-1", "name": "Desk lamp", "on": True}]}
    recorded = {"mcp": [{"server": "hue", "tool": "list_lights", "args": {}, "result": lights}]}
    (tmp_path / "lights.recording.json").write_text(json.dumps(recorded), encoding="utf-8")
    settings = tmp_path / "settings.brain.yaml"
    settings.write_text(
        "runtime:\n  role: brain\ntools:\n  plugins:\n    hue: {enabled: true}\n",
        encoding="utf-8",
    )
    case = {
        "id": "lights", "said": "要出门了灯关了", "at": AT.isoformat(), "zone": "UTC",
        "recording": "lights.recording.json",
    }
    model = ScriptedModel(  # the card ends the turn: no third request is made
        Reply(calls=(Call("mcp__hue__list_lights", {}, "list"),)),
        Reply(calls=(Call("mcp__hue__set_light", {"light_id": "lamp-1", "on": False}, "set"),)),
    )
    line = turns.run_one(
        case, model, "fake", 0, tmp_path, TABLE, Meter(None, TABLE), None, settings,
    )
    assert line["error"] is None, line["error"]
    listed = line["steps"][0]["calls"][0]
    assert listed["class"] == "recorded"
    assert json.loads(listed["result"]) == lights
    assert [c["tool"] for c in line["confirmations"]] == ["mcp__hue__set_light"]
    assert line["built"]["stubbed_providers"] == []


def test_the_listing_and_the_recordings_beside_directory_cases_are_not_cases(
    tmp_path: Path,
) -> None:
    """A cases directory holds the MCP listing and each case's recording next to the cases."""
    case = {"id": "lights", "said": "x", "at": AT.isoformat(), "zone": "UTC"}
    (tmp_path / "lights.json").write_text(json.dumps(case), encoding="utf-8")
    (tmp_path / "lights.recording.json").write_text(json.dumps({"mcp": []}), encoding="utf-8")
    (tmp_path / "mcp-tools.json").write_text(json.dumps(_HUE_LISTING), encoding="utf-8")
    assert steps.load_cases(tmp_path) == [case]


def _every_key_name() -> set[str]:
    """Every provider key the code or the shipped config names: a tool may need any of them."""
    texts = [p.read_text(encoding="utf-8") for p in (REPO / "jarvis").rglob("*.py")]
    texts.append((REPO / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    return {name for text in texts for name in _KEY_NAME.findall(text)}


def test_every_tool_a_brain_with_all_its_keys_offers_has_a_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unclassed tool stops a paid run before its first case; a new one stops here instead."""
    for name in _every_key_name():
        monkeypatch.setenv(name, "exam-placeholder-key")
    brain = tmp_path / "brain"
    brain.mkdir()
    (brain / "settings.yaml").write_text(
        "runtime:\n  role: brain\nhome:\n  weather:\n    latitude: 37.7749\n"
        "    longitude: -122.4194\n",
        encoding="utf-8",
    )
    item = Item({"id": "menu", "at": AT.isoformat(), "zone": "UTC"}, tmp_path / "menu.json")
    recording = Recording()
    with _Transport(recording, live=False), sandbox(item, recording, real_root=brain) as box:
        try:
            classes = classify(box.runtime.tool_registry)
        except SystemExit as exc:
            pytest.fail(str(exc))
    assert {"bus", "map_search", "weather", "web_search"} <= classes.keys()


class _Messages(BaseHTTPRequestHandler):
    """A stand-in for the Messages API: every request gets one short text answer."""

    bodies: ClassVar[list[dict[str, Any]]] = []
    keys: ClassVar[list[str | None]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(length)))
        type(self).keys.append(self.headers.get("x-api-key"))
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
    _Messages.keys = []
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


def _paid_run(tmp_path: Path, messages_api: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """``run-steps`` arguments for one built case on a preset served by the stand-in."""
    monkeypatch.setenv("EXAM_MESSAGES_KEY", "unset-below")  # undone at teardown, as are loads
    monkeypatch.delenv("EXAM_MESSAGES_KEY")
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path / "this-machine"))
    cases = tmp_path / "cases"
    cases.mkdir()
    case = {"id": "paid", "said": "anything on tonight", "at": AT.isoformat(), "zone": "UTC"}
    (cases / "paid.json").write_text(json.dumps(case), encoding="utf-8")
    config = tmp_path / "jarvis.yaml"
    config.write_text(json.dumps({"llm": {"provider": "anthropic", "max_retries": 0, "presets": {
        "haiku": {
            "provider": "anthropic", "model": "claude-haiku-5-5", "base_url": messages_api,
            "api_key_env": "EXAM_MESSAGES_KEY", "max_tokens": 64,
        },
    }}}), encoding="utf-8")
    (tmp_path / "root").mkdir()
    return [
        "tool_exam_steps", "run-steps", "--cases", str(cases), "--presets", "haiku",
        "--out", str(tmp_path / "out.jsonl"), "--root", str(tmp_path / "root"),
        "--config", str(config), "--cap-usd", "0.05",
        "--brain-settings", str(_brain_settings(tmp_path)),
    ]


def test_a_paid_run_takes_the_model_key_from_the_brain_snapshot(
    messages_api: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is only in ``--root``'s env, as a snapshot of the brain carries it."""
    argv = _paid_run(tmp_path, messages_api, monkeypatch)
    (tmp_path / "root" / "env").write_text("EXAM_MESSAGES_KEY=exam-snapshot-key\n", "utf-8")
    monkeypatch.setattr("sys.argv", argv)
    assert steps.main() == 0
    line = json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert line["error"] is None, line["error"]
    assert line["text"] == "Nothing yet."
    assert _Messages.keys == ["exam-snapshot-key"]


def test_a_paid_run_without_the_preset_key_stops_before_any_case(
    messages_api: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key anywhere: one stop naming the preset and its variable, and no case is booted."""
    monkeypatch.setattr("sys.argv", _paid_run(tmp_path, messages_api, monkeypatch))
    with pytest.raises(SystemExit) as stopped:
        steps.main()
    assert "no key for haiku (EXAM_MESSAGES_KEY)" in str(stopped.value)
    assert not (tmp_path / "out.jsonl").exists()
    assert _Messages.bodies == []
