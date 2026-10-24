"""The tool and plumbing layers of the exam (ADR 0317) hold: items, recorder, rules, baseline.

The synthetic items in ``tests/fixtures/tool_exam/items`` run through the real dispatcher and the
real ``decide()`` loop on a booted sandbox runtime, from recordings, at a frozen clock. Their
shrink-only ``baseline.json`` names the ones that fail today and why they should not (a bus after
midnight). Around them:
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


def _exchange(status: int, body: str, url: str = "http://x/forecast") -> dict[str, Any]:
    return {"method": "GET", "url": url, "status": status, "body": body}


def test_a_request_recorded_twice_is_answered_in_order_and_the_last_answer_repeats() -> None:
    """A read that works and then fails: the first answer first, the last one every time after."""
    recording = exam.Recording(
        {"http": [_exchange(200, '{"n": 1}'), _exchange(200, '{"n": 2}'), _exchange(503, "{}"),
                  _exchange(200, "other", "http://x/elsewhere")]},
    )
    with exam._Transport(recording, live=False):  # noqa: SLF001
        with urllib.request.urlopen("http://x/forecast") as first:
            assert json.load(first) == {"n": 1}
        with urllib.request.urlopen("http://x/elsewhere") as other:
            assert other.read() == b"other"  # another request has its own count
        with urllib.request.urlopen("http://x/forecast") as second:
            assert json.load(second) == {"n": 2}
        for _ in range(2):
            with pytest.raises(urllib.error.HTTPError) as down:
                urllib.request.urlopen("http://x/forecast")
            assert down.value.code == 503


def test_a_setup_step_lets_time_pass_only_when_it_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``after_s`` shifts the frozen clock after its own call and before the next one."""
    seen: list[str] = []

    class _Clock:
        def shift(self, seconds: float) -> None:
            seen.append(f"shift {seconds:g}")

    def dispatch(_box: Any, tool: str, args: dict[str, Any], _said: str) -> str:  # noqa: ANN401
        seen.append(f"{tool} {args}")
        return "{}"

    monkeypatch.setattr(exam, "dispatch", dispatch)
    item = exam.Item(
        {"id": "x", "said": "hi", "setup": [
            {"tool": "a", "args": {"n": 1}, "after_s": 7200}, {"tool": "b"},
            {"tool": "c", "after_s": 60},
        ]},
        Path("x.json"),
    )
    exam._setup(type("Box", (), {"clock": _Clock()})(), item)  # noqa: SLF001
    assert seen == ["a {'n': 1}", "shift 7200", "b {}", "c {}", "shift 60"]


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
        {"at": f"today {h:02d}:00", "rain_chance_pct": pct}
        for h, pct in zip(range(14, 24), (5, 5, 5, 20, 40, 45, 30, 10, 10, 10), strict=True)
    ]
    parts = {"rest_of_today": {"max_chance_pct": 45}, "earlier_today": {"rain_mm": 3.6}}
    return {"item": item, "out": {"hourly": hours, **parts, **changes}}


CHECKS = [
    ("weather_rest_of_today", {}, []),
    ("weather_rest_of_today", {"hourly": []}, ["hour today 14:00 is missing"]),
    (
        "weather_rest_of_today",
        {"hourly_pct": {"today 19:00": 80}},
        ["hour today 19:00: rain chance 80, recorded 45"],
    ),
    (
        "weather_rest_of_today",
        {"rest_of_today": {"max_chance_pct": 80}},  # the whole day's chance, not the hours left
        ["the peak of the hours left is 45, the result: 80"],
    ),
    (
        "weather_rest_of_today",
        {"earlier_today": {"rain_mm": 0.0}},
        ["3.6 mm fell before now, the result says 0.0"],
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


def _recorded(ident: str) -> tuple[dict[str, Any], exam.Recording]:
    item = json.loads((ITEMS / f"{ident}.json").read_text(encoding="utf-8"))
    return item, exam.Recording.load(ITEMS / f"{ident}.recording.json")


AFTER_DRIZZLE: dict[str, Any] = {
    "earlier_today": {"rain_mm": 1.0},
    "rest_of_today": {"max_chance_pct": 0, "rain_mm": 0.0},
    "tonight": {"low_c": 6.0, "low_at": "6:00"},
    "whole_day": {"rain_chance_pct": 70},
}
"""What the day of the drizzle item must hold: the recorded rows, added up by hand."""
TODAY_IN_PARTS = [
    ({}, []),
    (  # the day's own chance and amount are not those of the hours left
        {"rest_of_today": {"max_chance_pct": 70, "rain_mm": 3.5}},
        ["the highest chance left today is 0", "0.0 mm are forecast for the rest of today"],
    ),
    ({"earlier_today": {"rain_mm": 0.0}}, ["1.0 mm fell before now"]),
    ({"earlier_today": None}, ["1.0 mm fell before now"]),
    (  # the low of the daytime hours is not the night's
        {"tonight": {"low_c": 7.5, "low_at": "9:00"}},
        ["the night's low is 6 at 6:00"],
    ),
    ({"tonight": {"low_c": 6.0, "low_at": "06:00"}}, ["the night's low is 6 at 6:00"]),
    ({"whole_day": {"rain_chance_pct": 0}}, ["the day's row says 70%"]),
]


@pytest.mark.parametrize(("change", "expected"), TODAY_IN_PARTS)
def test_the_parts_of_today_are_added_up_from_the_recorded_hours(
    change: dict[str, Any],
    expected: list[str],
) -> None:
    """Rain so far, the rest of the day, the night's low and its hour come from the hourly rows."""
    item, recording = _recorded("weather-today-after-drizzle")
    got = CHECKERS["weather_today_in_parts"](item, recording, AFTER_DRIZZLE | change)
    assert all(any(e in m for m in got) for e in expected), got
    assert (got == []) == (expected == [])


SATURDAY = {"day": "Sat Oct 24", "date": "2026-10-24", "high_c": 15.0, "low_c": 8.0}
SUNDAY = {"day": "Sun Oct 25", "date": "2026-10-25", "high_c": 16.0, "low_c": 9.0}
NAMED_DAYS = [
    ({"days": [SATURDAY, SUNDAY]}, []),
    (
        {"days": [{**SATURDAY, "day": "Sat"}, SUNDAY]},
        ["2026-10-24 is named 'Sat', not 'Sat Oct 24'"],
    ),
    (  # Sunday's figures under Saturday's date
        {"days": [{**SATURDAY, "high_c": 16.0, "low_c": 9.0}, SUNDAY]},
        ["2026-10-24 high_c is 16.0, recorded 15.0", "2026-10-24 low_c is 9.0, recorded 8.0"],
    ),
    (
        {"days": [{**SATURDAY, "day": "Fri Oct 23", "date": "2026-10-23"}, SUNDAY]},
        ["the recording holds no daily row for 2026-10-23"],
    ),
    ({"days": [SATURDAY, {**SUNDAY, "day": "Sun Oct 25 2026"}]}, ["not 'Sun Oct 25'"]),
    ({"days": []}, ["the result holds no day row"]),
]


@pytest.mark.parametrize(("out", "expected"), NAMED_DAYS)
def test_every_day_of_a_result_is_named_by_its_date_and_holds_its_own_figures(
    out: dict[str, Any],
    expected: list[str],
) -> None:
    """Saturday is ``Sat Oct 24`` with Saturday's recorded high and low, never another day's."""
    item, recording = _recorded("weather-when-this-weekend")
    got = CHECKERS["weather_days_labelled"](item, recording, out)
    assert all(any(e in m for m in got) for e in expected), got
    assert (got == []) == (expected == [])


def test_today_and_tomorrow_are_named_so_and_other_days_by_their_date() -> None:
    """``today (Wed)`` and ``tomorrow (Thu)`` for the two days; a plain date for them is wrong."""
    item, recording = _recorded("weather-today-after-drizzle")
    today = {"day": "today (Wed)", "date": "2026-10-21", "high_c": 15.0, "low_c": 8.0}
    tomorrow = {"day": "tomorrow (Thu)", "date": "2026-10-22", "high_c": 16.0, "low_c": 9.0}
    check = CHECKERS["weather_days_labelled"]
    assert check(item, recording, {"whole_day": today, "tomorrow": tomorrow}) == []
    got = check(
        item, recording, {"whole_day": today, "tomorrow": {**tomorrow, "day": "Thu Oct 22"}}
    )
    assert got == ["2026-10-22 is named 'Thu Oct 22', not 'tomorrow (Thu)'"]


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
    passing = json.loads(
        (ITEMS / "schedule-write-then-read-rings.json").read_text(encoding="utf-8"),
    )
    # Fails by construction, so the test does not depend on a defect staying unfixed.
    failing = {**passing, "id": "never-holds", "expect": {"summary_must": ["no tool says this"]}}
    (tmp_path / "never-holds.json").write_text(json.dumps(failing), encoding="utf-8")
    result = exam.score(tmp_path)
    assert exam.verdict(result, detail=False) == ["FAIL never-holds"]
    assert exam.verdict(result)[0].startswith("FAIL never-holds: ")


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


def test_an_item_that_seats_job_mail_fails_when_the_recording_leaves_it_off(
    tmp_path: Path,
) -> None:
    """The mails are written through the ledger, so a runtime without job mail cannot take them."""
    item = json.loads((ITEMS / "jobs-eastern-time-is-his-clock.json").read_text(encoding="utf-8"))
    (tmp_path / "x.json").write_text(json.dumps({**item, "id": "x"}), encoding="utf-8")
    (tmp_path / "jobs.recording.json").write_text(
        json.dumps({"version": 1, "settings": {}, "env": [], "http": [], "mcp": []}),
        encoding="utf-8",
    )

    failed = exam.score(tmp_path)["failed"]["x"]

    assert failed
    assert any("no tool 'jobs'" in m or "leave job_mail off" in m for m in failed)


def test_the_block_d_check_compares_the_results_count_with_block_ds_own_line() -> None:
    """The same data gives the same number; a result that states another one is named."""
    item = next(i for i in exam.load_items(ITEMS) if i.id == "jobs-submitted-count-is-block-ds")
    recording = exam.Recording.load(ITEMS / "jobs.recording.json")
    with exam._Transport(recording, live=False), exam.sandbox(item, recording) as box:  # noqa: SLF001
        flag = {"block_d_submitted": True}
        assert exam._block_d_failures(box, flag, {"summary": "At least 7 submitted in all."}) == []  # noqa: SLF001
        wrong = exam._block_d_failures(box, flag, {"summary": "At least 9 submitted in all."})  # noqa: SLF001
        none = exam._block_d_failures(box, flag, {"summary": "Nothing counted."})  # noqa: SLF001
        off = exam._block_d_failures(box, {}, {"summary": "At least 9 submitted in all."})  # noqa: SLF001
    assert wrong == ["block D says at least 7 submitted, the result says 9"]
    assert len(none) == 1
    assert "None" in none[0]
    assert off == []


def _plumbing_copy(tmp_path: Path, name: str, change: dict[str, Any]) -> str:
    """The plumbing item ``name`` with ``change`` laid over it, written to ``tmp_path``."""
    item = json.loads((ITEMS / f"{name}.json").read_text(encoding="utf-8"))
    shutil.copy(ITEMS / item["recording"], tmp_path)
    (tmp_path / "x.json").write_text(json.dumps({**item, "id": "x", **change}), encoding="utf-8")
    return str(item["id"])


def test_a_plumbing_turn_can_press_the_waiting_card_and_the_card_the_clients_read_is_judged(
    tmp_path: Path,
) -> None:
    """``press`` is the button; ``card`` is what the card shows after the turn, key by key."""
    name = "plumbing-mail-an-edit-on-the-card-is-the-letter-that-is-sent"
    _plumbing_copy(tmp_path, name, {})
    assert exam.score(tmp_path)["failed"] == {}
    item = json.loads((ITEMS / f"{name}.json").read_text(encoding="utf-8"))
    wrong = [{**item["turns"][0], "card": {"letter": False, "args": {"to": ["x@example.com"]}}}]
    _plumbing_copy(tmp_path, name, {"turns": [*wrong, item["turns"][1]]})
    failed = exam.score(tmp_path)["failed"]["x"]
    assert any(m.startswith("after turn 0: card letter: expected False") for m in failed)
    assert any(m.startswith("after turn 0: card args: expected {'to': ['x@") for m in failed)
    _plumbing_copy(tmp_path, name, {"turns": [item["turns"][1]]})
    assert any("press: no card is waiting" in m for m in exam.score(tmp_path)["failed"]["x"])
    waiting = {"expect": {**item["expect"], "card": None}, "turns": item["turns"][:1]}
    _plumbing_copy(tmp_path, name, waiting)
    assert any("a card is waiting: mail" in m for m in exam.score(tmp_path)["failed"]["x"])
