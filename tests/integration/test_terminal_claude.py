"""ADR 0170 step 4f: the Agents page reads the terminal's Claude Code, not the brain's.

Claude Code's sessions are the owner's work on the Mac, and ``~/.claude`` and ``claude agents
--json`` are the Mac's. A brain asks its terminal for the board, a conversation and the reply, over
a real socket; the terminal runs the same code a one-machine daemon runs, over a fake ``claude``
and a fake home. What needs a model (whether a finished turn asks Allen something) stays on the
brain. With no terminal the page says the device is not there, and the brain reads no file of its
own.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.state import device_reads
from jarvis.state.device_tokens import pair_device
from jarvis.surface.claude_sessions import ClaudeSessions
from tests.support.terminal import Brain, Terminal, wait_for
from tests.support.terminal_claude import ASKING, BUSY, IDLE, WAITING, device_home

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.surface.terminal_link import TerminalHub

_VOLATILE = {"request", "compacting", "error"}
"""What ``ClaudeHooks.merge`` lays on each row: not the board's own."""


class _Jev:
    """The brain's side of the page: what it was asked, and the answer it gives."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, bool]] = []
        self.said: dict[str, list[float]] = {}

    def peek(self, session: str, tail: str, send: bool) -> bool | None:  # noqa: FBT001
        self.asked.append((session, tail, send))
        return "ship it" in tail

    def noted(self, session: str, times: list[float]) -> None:
        self.said[session] = times


class _Rig:
    """A brain whose Agents page asks its terminal, and a terminal over the fake home."""

    def __init__(self, tmp_path: Path, *, device_reads_claude: bool = True) -> None:
        self.jev = _Jev()
        self.token = pair_device(tmp_path, "macbook")
        log = bootstrap_runtime(tmp_path / "brain-root").event_log
        self.brain = Brain(tmp_path, log, deps=self._deps)
        self.terminal = Terminal(
            self.brain.url, self.token, store=None, repos=(),
            claude=ClaudeSessions() if device_reads_claude else None,
        )

    def _deps(self, _hub: TerminalHub) -> dict[str, Any]:
        return {
            "claude_sessions_read": True,
            "turn_end_peek": self.jev.peek, "turn_end_answered": self.jev.noted,
        }

    def __enter__(self) -> Self:
        self.brain.__enter__()
        self.terminal.__enter__()
        wait_for(lambda: bool(self.brain.hub.connected()), "the terminal never connected")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.terminal.stop()
        self.brain.__exit__()

    def disconnect(self) -> None:
        self.terminal.stop()
        wait_for(lambda: not self.brain.hub.connected(), "the terminal never left")

    def get(self, path: str) -> httpx.Response:
        return httpx.get(self.brain.url + path, headers={"Authorization": f"Bearer {self.token}"})

    def post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        return httpx.post(
            self.brain.url + path, json=body, headers={"Authorization": f"Bearer {self.token}"},
            timeout=20,
        )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The device's transcript of the idle session; the fake home is in place around it."""
    return device_home(tmp_path / "mac", monkeypatch)


def _board(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["session_id"]: {k: v for k, v in r.items() if k not in _VOLATILE} for r in rows}


def test_a_brains_board_is_the_terminals_and_the_asking_stays_on_the_brain(
    tmp_path: Path, home: Path,
) -> None:
    """The rows equal the one-machine rows; Jev is asked on the brain, about the terminal's tail."""
    del home
    reference = _Jev()
    one = ClaudeSessions(reference.peek, reference.noted).read()
    assert one["error"] is None
    assert {r["session_id"]: r["phase"] for r in one["sessions"]} == {
        IDLE: "done", BUSY: "working", WAITING: "needs_input",
    }
    assert reference.asked == [(IDLE, ASKING, True)]

    with _Rig(tmp_path) as rig:
        body = rig.get("/inherent/claude-sessions").json()
    assert body["error"] is None
    assert _board(body["sessions"]) == _board(one["sessions"])
    assert next(r for r in body["sessions"] if r["session_id"] == IDLE)["asks"] is True
    assert rig.jev.asked == reference.asked  # asked here, of the tail the terminal read
    assert rig.jev.said.keys() == reference.said.keys() == {IDLE, BUSY}


def test_a_conversation_and_a_reply_go_to_the_terminal_and_land_in_its_transcript(
    tmp_path: Path, home: Path,
) -> None:
    """The island's page and its reply: the same answers as one machine, typed on the device."""
    with _Rig(tmp_path) as rig:
        page = rig.get(f"/inherent/claude-sessions/{IDLE}/conversation").json()["messages"]
        assert page == [
            {"who": "you", "text": "fix the overlay"}, {"who": "it", "text": ASKING},
        ]
        assert rig.get("/inherent/claude-sessions/nope/conversation").status_code == 404
        busy = rig.post(f"/inherent/claude-sessions/{BUSY}/reply", {"text": "hi"})
        assert busy.status_code == 409  # the terminal's board says it is working
        blank = rig.post(f"/inherent/claude-sessions/{IDLE}/reply", {"text": " "})
        assert blank.status_code == 400
        sent = rig.post(f"/inherent/claude-sessions/{IDLE}/reply", {"text": "ship  it"})
        assert sent.json() == {"ok": True}
        after = rig.get(f"/inherent/claude-sessions/{IDLE}/conversation").json()["messages"]
    assert after[-1] == {"who": "you", "text": "ship it"}
    typed = {"type": "user", "message": {"content": "ship it"}}
    assert home.read_text().splitlines()[-1] == json.dumps(typed)  # on the device


def test_with_no_terminal_the_page_says_the_device_is_not_there(
    tmp_path: Path, home: Path,
) -> None:
    """The files exist, in this very process: a brain still reads none of them."""
    assert home.exists()
    with _Rig(tmp_path) as rig:
        assert rig.get("/inherent/claude-sessions").json()["sessions"] != []
        rig.disconnect()
        gone = rig.get("/inherent/claude-sessions").json()
        assert (gone["sessions"], gone["error"]) == ([], device_reads.NOT_CONNECTED)
        page = rig.get(f"/inherent/claude-sessions/{IDLE}/conversation")
        assert page.status_code == 502
        assert page.json()["detail"] == device_reads.NOT_CONNECTED
        reply = rig.post(f"/inherent/claude-sessions/{IDLE}/reply", {"text": "go"})
        assert (reply.status_code, reply.json()["detail"]) == (502, device_reads.NOT_CONNECTED)


def test_a_terminal_whose_config_does_not_read_claude_code_says_so(
    tmp_path: Path, home: Path,
) -> None:
    """The owner's consent is each machine's own setting (ADR 0046): off there, off here."""
    assert home.exists()
    with _Rig(tmp_path, device_reads_claude=False) as rig:
        body = rig.get("/inherent/claude-sessions").json()
        assert body["sessions"] == []
        assert "observer.claude_sessions.enabled" in body["error"]
        assert rig.get(f"/inherent/claude-sessions/{IDLE}/conversation").status_code == 502
