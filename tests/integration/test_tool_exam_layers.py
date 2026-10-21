"""The tool and plumbing layers of the exam (ADR 0317) hold: items, recorder, rules, baseline.

The synthetic items in ``tests/fixtures/tool_exam/items`` run through the real dispatcher and the
real ``decide()`` loop on a booted sandbox runtime, from recordings, at a frozen clock. Their
shrink-only ``baseline.json`` names the ones that fail today and why they should not (a bus after
midnight, a weather result with no summary). Around them:
the spec's result and error rules on a table of good and bad results, the recorder and replay
against a local server, and the baseline verdict.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.mcp_tools import McpServers
from scripts import tool_exam_tools as exam
from scripts.tool_exam_checkers import CHECKERS

if TYPE_CHECKING:
    from collections.abc import Iterator

ITEMS = Path(__file__).resolve().parents[1] / "fixtures" / "tool_exam" / "items"
KEY = "exam-credential-value-0123456789"
BEARER = "bearer-token-abcdefghijkl"


def test_every_synthetic_item_passes_or_is_in_the_shrink_only_baseline() -> None:
    """Both layers, one baseline: nothing fails outside it, nothing in it passes."""
    result = exam.score(ITEMS)
    assert result["items"] >= 10
    assert exam.verdict(result) == [], result["failed"]


RESULTS = [
    ('{"summary": "Nothing on tomorrow.", "items": []}', []),
    ('{"items": [], "summary": "Nothing."}', ["first field"]),
    ('{"summary": "Saved (id 3f2a1b4c-0d9e-4f3a-8b7c-1a2b3c4d5e6f)."}', ["summary holds a uuid"]),
    ('{"summary": "ok", "file": "/Users/a/notes/today.md"}', ["file holds a path"]),
    ('{"summary": "ok", "page": "<div class=\\"x\\">hi</div>"}', ["page holds a html"]),
    ('{"summary": "ok", "raw": "{\\"a\\": 1}"}', ["raw holds JSON inside JSON"]),
    ('{"summary": "ok", "sha": "' + "a" * 40 + '"}', ["sha holds a hash"]),
    ('{"summary": "' + "x" * 4100 + '"}', ["over about 4000"]),
    ('{"summary": "line one\\nline two"}', ["not one speakable line"]),
    (
        '{"error": "No stop by that name. Try another name.", '
        '"code": "not_found", "kind": "retry_other_args"}',
        [],
    ),
    (
        '{"error": "bad", "code": "invalid_arguments", "kind": "oops"}',
        ["not one of the four kinds"],
    ),
    ('{"error": "", "code": "closed", "kind": "impossible"}', ["error text is empty"]),
]


@pytest.mark.parametrize(("raw", "expected"), RESULTS)
def test_the_spec_rules_for_a_result_and_an_error(raw: str, expected: list[str]) -> None:
    """Each rule of docs/spec.html#tools has a result that breaks it and names it."""
    problems = exam.result_problems(raw)
    assert [any(e in p for p in problems) for e in expected] == [True] * len(expected), problems
    assert (problems == []) == (expected == [])
    if expected:
        with pytest.raises(AssertionError):
            exam.assert_tool_result(raw)


class _Provider(BaseHTTPRequestHandler):
    """A provider that echoes the key it was sent, so the recorder has something to scrub."""

    def do_GET(self) -> None:
        body = json.dumps({"hello": "world", "you_sent": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        """Silence the request log."""


@pytest.fixture
def provider() -> Iterator[str]:
    """A local provider and its base URL."""
    server = HTTPServer(("127.0.0.1", 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_recorder_scrubs_every_credential_and_replay_answers_without_the_network(
    provider: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Key in the URL, in a header, echoed in the body: none is in the file; replay is offline."""
    monkeypatch.setenv("EXAM_PROVIDER_API_KEY", KEY)
    recording = exam.Recording()
    live = exam._Transport(recording, live=True)  # noqa: SLF001
    request = urllib.request.Request(  # noqa: S310
        f"{provider}/v1/forecast?place=home&key={KEY}",
        headers={"Authorization": f"Bearer {BEARER}", "X-Api-Key": BEARER},
    )
    with live, urllib.request.urlopen(request) as reply:  # noqa: S310
        asked = json.load(reply)
    assert KEY in asked["you_sent"]
    saved = tmp_path / "r.recording.json"
    recording.save(saved, live.secrets)
    written = saved.read_text(encoding="utf-8")
    assert KEY not in written
    assert BEARER not in written
    assert recording.data["env"] == ["EXAM_PROVIDER_API_KEY"]

    replay = exam._Transport(exam.Recording.load(saved), live=False)  # noqa: SLF001
    with replay:
        with urllib.request.urlopen(  # noqa: S310
            f"{provider}/v1/forecast?place=home&key=another-placeholder",
        ) as again:
            assert json.load(again)["hello"] == "world"
        with pytest.raises(exam.RecordingMiss):
            urllib.request.urlopen(f"{provider}/v1/forecast?place=work")  # noqa: S310
        with pytest.raises(OSError, match="network blocked"):
            socket.create_connection(("127.0.0.1", 9))
    assert len(replay.misses) == 1


def test_only_the_model_request_opens_the_network_and_only_on_its_own_thread(
    provider: str,
) -> None:
    """Inside ``model_call`` this thread connects; another thread, and this one after, cannot."""
    port = int(provider.rsplit(":", 1)[1])
    replay = exam._Transport(exam.Recording(), live=False)  # noqa: SLF001
    elsewhere: list[BaseException] = []

    def other_thread() -> None:
        try:
            socket.create_connection(("127.0.0.1", port)).close()
        except OSError as exc:
            elsewhere.append(exc)

    with replay:
        with replay.model_call():
            socket.create_connection(("127.0.0.1", port)).close()
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join()
        with pytest.raises(OSError, match="network blocked"):
            socket.create_connection(("127.0.0.1", port))
    assert [str(e) for e in elsewhere] == ["network blocked: the tool layer runs from recordings"]


def test_a_recording_that_would_hold_a_key_is_not_written_and_the_refusal_names_no_value(
    tmp_path: Path,
) -> None:
    """The in-memory check: the value is in the bytes, so nothing is written."""
    recording = exam.Recording()
    recording.data["http"].append(
        {
            "method": "GET",
            "url": "http://x/y",
            "body_sha": "0",
            "status": 200,
            "body": f"echo {KEY}",
        }
    )
    with pytest.raises(SystemExit) as refused:
        recording.save(tmp_path / "r.json", {"EXAM_PROVIDER_API_KEY": KEY})
    assert "EXAM_PROVIDER_API_KEY" in str(refused.value)
    assert KEY not in str(refused.value)
    assert not (tmp_path / "r.json").exists()


def test_a_live_recording_keeps_out_a_key_saved_only_for_its_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is not in the shell, only among the root's saved keys: the file is still refused."""
    monkeypatch.setenv("EXAM_PROVIDER_API_KEY", "unset-below")  # undone at teardown, as is the load
    monkeypatch.delenv("EXAM_PROVIDER_API_KEY")
    root = tmp_path / "root"
    root.mkdir()
    (root / "env").write_text(f"EXAM_PROVIDER_API_KEY={KEY}\n", encoding="utf-8")
    (root / "settings.yaml").write_text(f"transit:\n  stop: {KEY}\n", encoding="utf-8")
    item = tmp_path / "now.json"
    now = {"id": "now", "tool": "kept", "at": "2026-10-17T20:14:00-07:00", "zone": "UTC"}
    item.write_text(json.dumps(now), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        exam.main(["record", str(item), "--root", str(root)])
    assert "EXAM_PROVIDER_API_KEY" in str(refused.value)
    assert KEY not in str(refused.value)
    assert not (tmp_path / "now.recording.json").exists()


def test_an_mcp_call_is_answered_from_the_recording_or_fails_the_item() -> None:
    """The MCP client's tool-call boundary: a recorded call replays, an unrecorded one misses."""
    recording = exam.Recording(
        {
            "mcp": [
                {
                    "server": "microsoft",
                    "tool": "list-todo-tasks",
                    "args": {"list": "a"},
                    "result": {"value": [{"title": "Pay rent", "status": "notStarted"}]},
                }
            ]
        }
    )
    replay = exam._Transport(recording, live=False)  # noqa: SLF001
    servers: Any = McpServers.__new__(McpServers)
    with replay:
        assert (
            servers.call("microsoft", "list-todo-tasks", {"list": "a"})["value"][0]["title"]
            == "Pay rent"
        )
        with pytest.raises(exam.RecordingMiss):
            servers.call("microsoft", "list-todo-tasks", {"list": "b"})
    assert replay.misses == ['mcp microsoft.list-todo-tasks {"list": "b"}']


def _weather(**changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """The rest-of-today fixture's result, then ``changes``."""
    item = json.loads((ITEMS / "weather-rest-of-today.json").read_text(encoding="utf-8"))
    hours = [
        {"at": f"2026-10-12 {h:02d}:00", "rain_chance_pct": pct}
        for h, pct in zip(range(14, 24), (5, 5, 5, 20, 40, 45, 30, 10, 10, 10), strict=True)
    ]
    return {"item": item, "out": {"hourly": hours, **changes}}


CHECKS = [
    ("weather_rest_of_today", {}, []),
    ("weather_rest_of_today", {"hourly": []}, ["hour 2026-10-12 14:00 is missing"]),
    (
        "weather_rest_of_today",
        {"hourly_pct": {"2026-10-12 19:00": 80}},
        ["hour 2026-10-12 19:00: rain chance 80, recorded 45"],
    ),
]


@pytest.mark.parametrize(("name", "change", "expected"), CHECKS)
def test_a_reference_checker_derives_the_facts_from_the_recording(
    name: str,
    change: dict[str, Any],
    expected: list[str],
) -> None:
    """The recorded forecast, not the tool, says which hours are left and how likely rain is."""
    case = _weather()
    out = case["out"]
    for key, value in change.items():
        if key == "hourly_pct":
            for row in out["hourly"]:
                row["rain_chance_pct"] = value.get(row["at"], row["rain_chance_pct"])
        else:
            out[key] = value
    recording = exam.Recording.load(ITEMS / "weather-rest-of-today.recording.json")
    got = CHECKERS[name](case["item"], recording, out)
    assert all(any(e in m for m in got) for e in expected), got
    assert (got == []) == (expected == [])


def test_the_baseline_verdict_names_a_regression_a_fixed_item_and_a_stale_id(
    tmp_path: Path,
) -> None:
    """A failure outside the baseline fails; a listed item that passes must leave the list."""
    copied = Path(shutil.copy(ITEMS / "schedule-write-then-read-rings.json", tmp_path))
    passing = json.loads(copied.read_text(encoding="utf-8"))
    # Fails by construction, so the test does not depend on a defect staying unfixed.
    failing = {**passing, "id": "never-holds", "expect": {"summary_must": ["no tool says this"]}}
    (tmp_path / "never-holds.json").write_text(json.dumps(failing), encoding="utf-8")
    (tmp_path / "baseline.json").write_text(
        json.dumps(["schedule-write-then-read-rings", "gone-item"]),
        encoding="utf-8",
    )
    lines = exam.verdict(exam.score(tmp_path))
    assert any(ln.startswith("FAIL never-holds") for ln in lines)
    assert "schedule-write-then-read-rings: passes now; remove it from the baseline" in lines
    assert "gone-item: no such item; remove it from the baseline" in lines


OUT = {
    "summary": "Two things.",
    "items": [
        {"id": "a1", "what": "Dentist", "when": "tomorrow 3:30 pm", "rings": ["-60m"]},
        {"id": "b2", "what": "Call mum", "when": "Tue 9:00 am"},
    ],
}
# columns: the path, the op, its value, whether the check holds
RESULT_CHECKS = [
    ("$", "mentions", "dentist", True),
    ("$", "mentions", ["dentist", "9:00"], True),
    ("$", "mentions", "plumber", False),
    ("$", "not_mentions", "plumber", True),
    ("summary", "eq", "Two things.", True),
    ("items", "len", 2, True),
    ("items.0.what", "eq", "Dentist", True),
    ("items[1].what", "eq", "Call mum", True),
    ("items[?what~dent].when", "mentions", "3:30", True),
    ("items[?what=Dentist].rings", "contains", "-60m", True),
    ("items[?what~call].rings", "exists", None, False),
    ("items[?what~call].rings", "absent", None, True),
    ("items[?what~plumber]", "exists", None, False),
    ("items[*].id", "in", ["a1"], True),
    ("items[?what~dent]", "ne", [], True),
    ("items[?what~dent].when", "matches", r"\d:\d\d pm", True),
]


@pytest.mark.parametrize(("path", "op", "value", "holds"), RESULT_CHECKS)
def test_a_result_check_selects_by_path_filter_and_op(
    path: str, op: str, value: object, *, holds: bool
) -> None:
    """The path grammar (``$``, keys, indexes, ``[?field~text]``) and each op, on one result."""
    why = exam.check_result(OUT, {"path": path, "op": op, "value": value})
    assert (why is None) == holds, why


def test_an_unknown_op_is_a_failure_of_the_item_not_a_crash() -> None:
    """The runner reports it and goes on."""
    assert "unknown result op" in str(exam.check_result(OUT, {"path": "$", "op": "bogus"}))


def test_a_held_set_is_reported_by_id_alone(tmp_path: Path) -> None:
    """The failure's reason would quote the item, so the held set's verdict does not carry it."""
    shutil.copy(ITEMS / "weather-rest-of-today.json", tmp_path)
    shutil.copy(ITEMS / "weather-rest-of-today.recording.json", tmp_path)
    result = exam.score(tmp_path)
    assert exam.verdict(result, detail=False) == ["FAIL weather-rest-of-today"]
    assert exam.verdict(result)[0].startswith("FAIL weather-rest-of-today: ")


def test_a_provider_call_the_recording_lacks_fails_the_item(tmp_path: Path) -> None:
    """The weather item with an empty recording: the call is missing, so the item fails."""
    shutil.copy(ITEMS / "weather-rest-of-today.json", tmp_path)
    (tmp_path / "weather-rest-of-today.recording.json").write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "home": {
                        "weather": {"latitude": 37.77, "longitude": -122.42, "country_code": "US"}
                    }
                },
                "env": [],
                "http": [],
                "mcp": [],
            }
        ),
        encoding="utf-8",
    )
    failed = exam.score(tmp_path)["failed"]["weather-rest-of-today"]
    assert any(
        m.startswith("provider call missing from the recording: GET https://weatherkit")
        for m in failed
    )


def test_an_unknown_tool_fails_the_item_instead_of_passing_it(tmp_path: Path) -> None:
    """A renamed tool is a failing item, not a vacuous pass."""
    item = json.loads((ITEMS / "schedule-write-then-read-rings.json").read_text(encoding="utf-8"))
    (tmp_path / "x.json").write_text(
        json.dumps({**item, "id": "x", "tool": "no_such_tool"}), encoding="utf-8"
    )
    failed = exam.score(tmp_path)["failed"]["x"]
    assert failed
    assert any("no tool" in m for m in failed)
