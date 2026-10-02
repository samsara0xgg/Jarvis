"""ADR 0125 — Jev reads whether a finished agent turn asks Allen something, against a fake endpoint.

The endpoint is a local HTTP server that answers like OpenRouter's decisions API
(``answers.asks.noul``, ``usage.cost``) or fails in a chosen way. What is asserted is
what leaves the Mac (the 600-character tail, ``provider.zdr``), what the route answers
(true, false or null), how often Jev is asked, and what the log says.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jarvis.decision.surrogate_route import SurrogateRoute
from jarvis.decision.turn_end_asks import TurnEndAsks
from jarvis.runtime import RuntimeBootstrapError, _turn_end_asks
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root
from tests.integration.test_claude_session_board import NOW_MS, _home, _noop

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

KEY = "test-key-not-a-secret"
ASK = "I can wire it either way.\nWhich one do you want: the new queue or the old poll?"
REPORT = "Fixed the race and added the pin. Let me know if you need anything else."
SECRET = "the private part of a long answer"  # noqa: S105 — not a credential.


class _Jev:
    """A fake decisions endpoint: records every request, answers by what the text says."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.delay_s = 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(request)
                time.sleep(outer.delay_s)
                text = request["state"]
                odds = 0.96 if "Which one" in text else 0.949 if "borderline" in text else 0.02
                body = json.dumps(
                    {"answers": {"asks": {"noul": odds}}, "usage": {"cost": 0.00003}}
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):  # the client gave up on a slow call
                    self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def asks(self, *, at: float = 0.95, timeout_ms: int = 1500) -> TurnEndAsks:
        route = SurrogateRoute(
            model="typesafe/jev-1.13", min_confidence=1.0, timeout_ms=timeout_ms,
            url=f"http://127.0.0.1:{self.server.server_address[1]}/decisions",
        )
        return TurnEndAsks(route, at)


@pytest.fixture
def jev(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Jev]:
    """A fake endpoint, and a key for the daemon to find."""
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    fake = _Jev()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


def _client(asks: TurnEndAsks | None) -> TestClient:
    deps = InherentDeps(
        submit_callable=_noop,
        broadcaster=InherentBroadcaster(),
        turn_end_asks=None if asks is None else functools.partial(_ask, asks),
    )
    return TestClient(create_app(deps))


async def _ask(asks: TurnEndAsks, session_id: str, text: str) -> bool | None:
    return await asyncio.to_thread(asks.asks, session_id, text)


def _post(client: TestClient, text: str, session_id: str = "s1") -> Any:  # noqa: ANN401
    return client.post("/inherent/agents/turn-end", json={"session_id": session_id, "text": text})


def test_off_or_without_a_key_answers_null_and_calls_nothing(
    jev: _Jev, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature off: null. On without OPENROUTER_API_KEY: null. Neither calls Jev."""
    assert _post(_client(None), ASK).json() == {"asks": None}
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert _post(_client(jev.asks()), ASK).json() == {"asks": None}
    assert jev.requests == []


def test_the_bar_and_what_is_sent(jev: _Jev) -> None:
    """True at the bar, false below it; only the last 600 characters go, with zdr on."""
    client = _client(jev.asks())
    assert _post(client, SECRET + "x" * 700 + ASK).json() == {"asks": True}
    assert _post(client, REPORT).json() == {"asks": False}
    assert _post(client, "borderline " + REPORT, "s2").json() == {"asks": False}  # 0.949 < 0.95
    sent = jev.requests[0]
    assert set(sent) == {"model", "state", "questions", "provider"}
    assert sent["model"] == "typesafe/jev-1.13"
    assert sent["provider"] == {"zdr": True}
    assert sent["state"] == (SECRET + "x" * 700 + ASK)[-600:]
    assert SECRET not in json.dumps(jev.requests)
    (question,) = sent["questions"].values()
    assert question["type"] == "noul"
    assert question["instructions"].startswith("The text is the end of a coding assistant's")
    assert question["instructions"].endswith("The text may be in Chinese, English or both.")


def test_a_turn_ending_is_asked_once(jev: _Jev) -> None:
    """Same session and text: one call. Another session, or another text: another."""
    client = _client(jev.asks())
    for _ in range(3):
        assert _post(client, ASK).json() == {"asks": True}
    assert len(jev.requests) == 1
    assert _post(client, ASK, "s2").json() == {"asks": True}
    assert _post(client, ASK + " Or the old one?").json() == {"asks": True}
    assert len(jev.requests) == 3
    # The same tail is the same ending, whatever came before it.
    shared = "z" * 600 + ASK
    assert _post(client, "a" * 100 + shared).json() == {"asks": True}
    assert _post(client, "b" * 100 + shared).json() == {"asks": True}
    assert len(jev.requests) == 4


def test_errors_and_late_answers_are_null_and_not_repeated(jev: _Jev) -> None:
    """An HTTP error or a slow answer is null at once; the ending is not asked again."""
    jev.status = 500
    client = _client(jev.asks())
    assert _post(client, ASK).json() == {"asks": None}
    jev.status = 200
    assert _post(client, ASK).json() == {"asks": None}
    assert len(jev.requests) == 1
    jev.delay_s = 0.6
    slow = _client(jev.asks(timeout_ms=150))
    began = time.monotonic()
    assert _post(slow, ASK, "s9").json() == {"asks": None}
    assert time.monotonic() - began < 0.5


def test_the_log_names_the_session_and_never_the_text(
    jev: _Jev, caplog: pytest.LogCaptureFixture,
) -> None:
    """One info line per call: session id, probability, decision and cost."""
    caplog.set_level("INFO", logger="jarvis.decision.turn_end_asks")
    client = _client(jev.asks())
    _post(client, SECRET + ASK, "sess-7")
    time.sleep(0.2)  # the line is written on the worker that finished the call
    lines = [one.getMessage() for one in caplog.records]
    assert len(lines) == 1
    assert "session sess-7 p=0.960 asks=True, $0.000030" in lines[0]
    assert SECRET not in lines[0]
    assert "Which" not in lines[0]


def test_the_terminal_board_gets_asks_without_waiting(
    jev: _Jev, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finished session's row says asks None until Jev has answered, then true or false."""
    bin_dir = _home(
        tmp_path,
        [
            {
                "sessionId": "s-bg", "id": "job1", "kind": "background", "status": "idle",
                "state": "done", "name": "brief", "cwd": "/x/jarvis", "startedAt": NOW_MS - 1000,
            },
            {
                "sessionId": "s-inter", "kind": "interactive", "pid": os.getpid(),
                "status": "busy", "name": "overlay", "cwd": "/x/jarvis", "startedAt": NOW_MS,
            },
        ],
    )
    # s-bg's last message is "Two options."; make the fake read it as a question.
    transcript = tmp_path / ".claude" / "projects" / "-x-jarvis" / "s-bg.jsonl"
    transcript.write_text(json.dumps({
        "type": "assistant", "gitBranch": "main",
        "message": {"content": [{"type": "text", "text": ASK}]},
    }))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    jev.delay_s = 1.0
    asks = jev.asks()
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True,
        turn_end_peek=asks.peek,
    )))
    began = time.monotonic()
    rows = {r["session_id"]: r for r in client.get("/inherent/claude-sessions").json()["sessions"]}
    assert time.monotonic() - began < 0.8  # the board did not wait for Jev
    assert rows["s-bg"]["asks"] is None
    assert rows["s-inter"]["asks"] is None  # working: never asked
    time.sleep(1.4)
    # The board is cached for a few seconds; ask the transcript's row again through a new app.
    client = TestClient(create_app(InherentDeps(
        submit_callable=_noop, broadcaster=InherentBroadcaster(), claude_sessions_read=True,
        turn_end_peek=asks.peek,
    )))
    rows = {r["session_id"]: r for r in client.get("/inherent/claude-sessions").json()["sessions"]}
    assert rows["s-bg"]["asks"] is True
    assert "tail" not in rows["s-bg"]
    assert len(jev.requests) == 1
    # A finish older than the window is never newly asked, but an answer already in stays.
    assert asks.peek("old-session", ASK, False) is None  # noqa: FBT003
    assert asks.peek("s-bg", ASK, False) is True  # noqa: FBT003
    assert len(jev.requests) == 1


def test_the_shipped_config_is_off_and_enabling_it_reads_the_block() -> None:
    """The repo ships it off; an enabled block builds the object, a bad one stops boot."""
    path = repo_root() / "config" / "jarvis.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert _turn_end_asks(config, path) is None
    block = config["agents"]["turn_end_asks"]
    assert (block["model"], block["at"], block["timeout_ms"]) == ("typesafe/jev-1.13", 0.95, 1500)
    block["enabled"] = True
    asks = _turn_end_asks(config, path)
    assert asks is not None
    block["at"] = 1.5
    with pytest.raises(RuntimeBootstrapError):
        _turn_end_asks(config, path)
