"""The tool exam answers httpx requests from a recording, like ``urlopen`` ones (ADR 0317).

``web_search`` and ``web_fetch`` reach their providers and pages through httpx. A replayed item
must answer them from the recording, fail the item on a call the recording lacks, and let the item
say what the providers were sent.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from scripts import tool_exam_tools as exam
from tests.support.net import start_http_server, stop_http_server

if TYPE_CHECKING:
    from pathlib import Path

URL = "https://api.provider.example/search"


def _recording(*entries: dict[str, Any]) -> exam.Recording:
    base = {"method": "POST", "url": URL, "status": 200, "content_type": "application/json"}
    return exam.Recording({"http": [{**base, **one} for one in entries]})


def test_an_httpx_call_is_answered_from_the_recording_or_fails_the_item() -> None:
    """The same JSON in another key order finds its answer; another body is a miss."""
    recording = _recording(
        {"request": json.dumps({"query": "a", "topic": "news"}), "body": '{"hit": 1}'},
    )
    replay = exam._Transport(recording, live=False)  # noqa: SLF001
    with replay:
        same = httpx.post(URL, json={"topic": "news", "query": "a"})
        assert (same.status_code, same.json()) == (200, {"hit": 1})
        assert same.headers["content-type"] == "application/json"
        with pytest.raises(exam.RecordingMiss):
            httpx.post(URL, json={"query": "b", "topic": "news"})
    assert len(replay.misses) == 1
    assert replay.misses[0].startswith(f"POST {URL} ")
    assert [r["body"] for r in replay.requests] == [
        {"topic": "news", "query": "a"}, {"query": "b", "topic": "news"},
    ]


def test_an_entry_can_answer_any_body_or_be_told_from_another_by_what_the_body_holds() -> None:
    """A model chooses its own query, so an entry may take any; a pair is told apart by content."""
    recording = _recording(
        {"any_body": True, "body": '{"which": "any"}', "body_lacks": ["paused"]},
        {"any_body": True, "body": '{"which": "second"}', "body_has": ["paused"]},
    )
    with exam._Transport(recording, live=False):  # noqa: SLF001
        first = httpx.post(URL, json={"query": "whatever the model asked"})
        second = httpx.post(URL, json={"query": "x", "messages": ["paused"]})
    assert (first.json()["which"], second.json()["which"]) == ("any", "second")


def test_a_recorded_page_gives_its_status_content_type_and_redirect() -> None:
    """A redirect keeps the location header its follower needs; an error keeps its status."""
    get = {"method": "GET", "content_type": "text/html"}
    recording = _recording(
        {**get, "url": "https://site.example/old", "status": 301, "body": "",
         "headers": {"location": "https://site.example/new"}},
        {**get, "url": "https://site.example/gone", "status": 404, "body": "<p>nothing</p>"},
    )
    with exam._Transport(recording, live=False):  # noqa: SLF001
        moved = httpx.get("https://site.example/old")
        gone = httpx.get("https://site.example/gone")
    assert (moved.status_code, moved.headers["location"]) == (301, "https://site.example/new")
    assert (gone.status_code, gone.text, gone.headers["content-type"]) == (
        404, "<p>nothing</p>", "text/html",
    )


class _Provider(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers.get("Content-Length") or 0)
        sent = json.loads(self.rfile.read(size))
        body = json.dumps({"you_asked": sent["query"]}).encode()
        self.send_response(302)
        self.send_header("Content-Type", "application/json")
        self.send_header("Location", "https://elsewhere.example/next")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


def test_a_live_httpx_call_is_recorded_and_then_replays_without_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording keeps the answer, its content type and location; a replay needs no server."""
    monkeypatch.setenv("EXAM_PROVIDER_API_KEY", "exam-credential-value-0123456789")
    server = start_http_server(_Provider)
    try:
        url = f"http://127.0.0.1:{server.server_port}/search"
        recording = exam.Recording()
        live = exam._Transport(recording, live=True)  # noqa: SLF001
        with live, httpx.Client(trust_env=False) as client:  # no proxy between us and 127.0.0.1
            made = client.post(url, json={"query": "oat milk"})
        assert made.json() == {"you_asked": "oat milk"}
    finally:
        stop_http_server(server)
    saved = tmp_path / "r.recording.json"
    recording.save(saved, live.secrets)
    (entry,) = json.loads(saved.read_text(encoding="utf-8"))["http"]
    assert (entry["status"], entry["content_type"]) == (302, "application/json")
    assert entry["headers"] == {"location": "https://elsewhere.example/next"}
    with exam._Transport(exam.Recording.load(saved), live=False):  # noqa: SLF001
        again = httpx.post(url, json={"query": "oat milk"}, follow_redirects=False)
    assert (again.status_code, again.json()) == (302, {"you_asked": "oat milk"})
    assert again.headers["location"] == "https://elsewhere.example/next"


SENT = [
    {"method": "POST", "url": f"{URL}?x=1", "body": {"topic": "news", "query": "a"}},
    {"method": "POST", "url": URL, "body": {"topic": "general", "query": "b"}},
]


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        pytest.param(
            {"url": "provider.example", "count": 2,
             "body": [{"path": "topic", "op": "eq", "value": "news"}]},
            [], id="the first request holds what it should",
        ),
        pytest.param(
            {"url": "provider.example", "nth": -1,
             "body": [{"path": "topic", "op": "eq", "value": "general"},
                      {"path": "time_range", "op": "absent"}]},
            [], id="the last request, and a field it must not carry",
        ),
        pytest.param(
            {"url": "provider.example", "nth": 1,
             "body": [{"path": "topic", "op": "eq", "value": "news"}]},
            ["request 1 to provider.example: result topic eq 'news': reached [\"general\"]"],
            id="a field that is not what was expected",
        ),
        pytest.param(
            {"url": "provider.example", "count": 3},
            ["requests to provider.example: expected 3, got 2"], id="how many",
        ),
        pytest.param(
            {"url": "other.example", "body": [{"path": "topic", "op": "exists"}]},
            ["request 0 to other.example: only 0 were made"], id="no such request",
        ),
    ],
)
def test_an_item_says_what_the_providers_must_have_been_sent(
    check: dict[str, Any], expected: list[str],
) -> None:
    """``expect.requests`` is judged over what the transport saw, by the result checks' own ops."""
    assert exam._request_failures([check], SENT) == expected  # noqa: SLF001
