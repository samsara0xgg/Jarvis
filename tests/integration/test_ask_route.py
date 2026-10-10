"""`POST /inherent/ask`: one question in, the turn's final answer out, for a Siri shortcut.

The real app behind the real guard, the real `submit` write, the real response watcher and the real
reader of a turn's rows run against an on-disk Event Log. The decision pipeline is a stand-in thread
that answers each `surface.user_intent` with the rows the real one writes (a wait line, then the
answer; a failure; a cancel; nothing at all), so no model is called.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.runtime import _new_turn_id, inherent_loop
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.plugin_settings import local_key_matches
from jarvis.surface import inherent_server
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

LOCAL_KEY = "local-key-for-this-test"
DEVICE_TOKEN = "paired-device-token-for-this-test"  # noqa: S105 — a stand-in, not a secret.
PHONE = ("100.64.0.7", 50123)  # a tailnet address: not on loopback
ANSWER = "It is noon."
DETAILS = "Noon in Vancouver, 12:00 PDT, Friday 10 October."


def _started(conn: sqlite3.Connection, turn_id: str, response_id: str, phase: str) -> None:
    emit_event(
        conn,
        type="response.started",
        payload={
            "response_id": response_id, "response_group_id": f"G-{turn_id}", "turn_id": turn_id,
            "phase": phase, "channel": "both", "emission_mode": "full_text",
            "output_risk_class": "low", "required_gate_mode": "pre_emit", "policy_hash": "h",
            "active_subject_ref": None, "evidence_snapshot_hash": "h",
        },
        correlation={"turn_id": turn_id},
    )


def _opened(conn: sqlite3.Connection, turn_id: str, response_id: str, phase: str) -> None:
    emit_event(
        conn,
        type="surface.response_open",
        payload={
            "turn_id": turn_id, "query": "q", "kind": "text", "response_id": response_id,
            "phase": phase,
        },
        correlation={"turn_id": turn_id},
    )


def _emitted(
    conn: sqlite3.Connection, turn_id: str, response_id: str, phase: str, voice: str,
    **more: object,
) -> None:
    payload: dict[str, object] = {
        "turn_id": turn_id, "text": voice, "voice_text": voice, "document_text": voice,
        "response_id": response_id, "phase": phase, "attention_channel": "voice_notify",
        **more,
    }
    emit_event(
        conn, type="surface.response_emitted", payload=payload, correlation={"turn_id": turn_id},
    )


def _terminal(conn: sqlite3.Connection, kind: str, turn_id: str, response_id: str) -> None:
    emit_event(
        conn,
        type=kind,
        payload={
            "response_id": response_id, "response_group_id": f"G-{turn_id}", "turn_id": turn_id,
            "reason": "turn_raised" if kind == "response.failed" else "user_stop",
        },
        correlation={"turn_id": turn_id},
    )


def _wait_line(conn: sqlite3.Connection, turn_id: str) -> None:
    _started(conn, turn_id, f"W-{turn_id}", "commentary")
    _opened(conn, turn_id, f"W-{turn_id}", "commentary")
    _emitted(conn, turn_id, f"W-{turn_id}", "commentary", "One moment.")


def _script(conn: sqlite3.Connection, text: str, turn_id: str) -> None:
    """What the stand-in pipeline writes for one question, by the words asked."""
    final = f"R-{turn_id}"
    if text == "never":
        return
    if text == "wait line then answer":
        _wait_line(conn, turn_id)
        time.sleep(0.4)
        _terminal(conn, "response.cancelled", turn_id, f"W-{turn_id}")  # the line taken back
        time.sleep(0.3)  # a re-read lands here: the turn has neither answer nor failure yet
        _started(conn, turn_id, final, "final")
        _opened(conn, turn_id, final, "final")
        _emitted(
            conn, turn_id, final, "final", ANSWER, document_text=DETAILS, written_apart=True,
        )
    elif text == "plain answer":
        _started(conn, turn_id, final, "final")
        _emitted(conn, turn_id, final, "final", ANSWER)
    elif text == "run fails":
        _wait_line(conn, turn_id)
        _started(conn, turn_id, final, "final")
        _terminal(conn, "response.failed", turn_id, final)
    elif text == "run cancelled":
        _started(conn, turn_id, final, "final")
        _terminal(conn, "response.cancelled", turn_id, final)
    elif text == "turn fails":
        emit_event(
            conn, type="turn.failed",
            payload={
                "turn_id": turn_id, "exception_repr": "KeyError('k')", "reason": "missing_key",
            },
            correlation={"turn_id": turn_id},
        )
    else:  # the typed text itself comes back
        _started(conn, turn_id, final, "final")
        _emitted(conn, turn_id, final, "final", text)


class _Pipeline(threading.Thread):
    """Answers each `surface.user_intent` the way `_script` says; no model, no daemon."""

    def __init__(self, path: Path) -> None:
        super().__init__(daemon=True)
        self.path = path
        self.stop = threading.Event()
        self.channels: dict[str, str] = {}

    def run(self) -> None:
        conn = open_event_log(self.path)
        seen: set[str] = set()
        while not self.stop.wait(0.005):
            for intent in iter_events_of_types(conn, ("surface.user_intent",)):
                turn_id = str(intent.payload["turn_id"])
                if turn_id not in seen:
                    seen.add(turn_id)
                    self.channels[turn_id] = str(intent.payload.get("channel"))
                    threading.Thread(  # one turn each: a slow answer holds up no other
                        target=self._answer, args=(str(intent.payload["transcript"]), turn_id),
                        daemon=True,
                    ).start()
        conn.close()

    def _answer(self, text: str, turn_id: str) -> None:
        conn = open_event_log(self.path)
        try:
            _script(conn, text, turn_id)
        finally:
            conn.close()


class _Daemon:
    """The ask route's app on an event loop with the response watcher beside it."""

    def __init__(self, tmp_path: Path) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.submitted: list[str] = []
        self.pipeline = _Pipeline(self.paths.event_log)

        def submit(text: str) -> str:
            """The daemon's `submit_callable`: a fresh turn id, one `surface.user_intent` row."""
            self.submitted.append(text)
            turn_id = _new_turn_id()
            conn = open_event_log(self.paths.event_log)
            try:
                emit_surface_user_intent(conn, transcript=text, turn_id=turn_id)
            finally:
                conn.close()
            return turn_id

        self.broadcaster = InherentBroadcaster()
        self.app = create_app(InherentDeps(
            submit_callable=submit, broadcaster=self.broadcaster,
            ask_outcome=functools.partial(inherent_loop._ask_outcome, self.paths.event_log),  # noqa: SLF001
        ))
        require_local_key(
            self.app,
            functools.partial(local_key_matches, LOCAL_KEY),
            device_token_matches=lambda token: token == DEVICE_TOKEN,
        )

    def intents(self) -> list[tuple[str, str]]:
        """`(channel, transcript)` of every question that reached the log."""
        conn = open_event_log(self.paths.event_log)
        try:
            return [
                (str(e.payload["channel"]), str(e.payload["transcript"]))
                for e in iter_events_of_types(conn, ("surface.user_intent",))
            ]
        finally:
            conn.close()


@contextlib.asynccontextmanager
async def _serving(tmp_path: Path) -> AsyncIterator[tuple[_Daemon, httpx.AsyncClient]]:
    daemon = _Daemon(tmp_path)
    conn = open_event_log(daemon.paths.event_log)
    watcher = asyncio.create_task(
        inherent_loop._response_watcher(  # noqa: SLF001
            SimpleNamespace(conn=conn), daemon.broadcaster, poll_interval_s=0.01,  # type: ignore[arg-type]
        ),
    )
    daemon.pipeline.start()
    transport = httpx.ASGITransport(app=daemon.app, client=PHONE)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {DEVICE_TOKEN}"}, timeout=30,
        ) as client:
            yield daemon, client
    finally:
        daemon.pipeline.stop.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        conn.close()


def _run(tmp_path: Path, scenario: Callable[[_Daemon, httpx.AsyncClient], Any]) -> None:
    async def main() -> None:
        async with _serving(tmp_path) as (daemon, client):
            await scenario(daemon, client)

    asyncio.run(main())


@pytest.fixture(autouse=True)
def _quick_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seconds, not the route's 25; no safety re-read, so only the watcher's wake can answer."""
    monkeypatch.setattr(inherent_server, "_ASK_WAIT_S", 5.0)
    monkeypatch.setattr(inherent_server, "_ASK_RECHECK_S", 60.0)


def test_the_final_answer_comes_back_the_moment_it_is_written(tmp_path: Path) -> None:
    """Spoken and written parts of the turn's answer, typed in on the silent channel."""

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        started = time.monotonic()
        response = await client.post("/inherent/ask", json={"text": "  plain answer  "})
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"turn_id", "spoken", "written"}
        assert (body["spoken"], body["written"]) == (ANSWER, "")  # nothing left out by the voice
        assert time.monotonic() - started < 2  # woken by the answer, not by a poll
        assert daemon.submitted == ["plain answer"]
        assert daemon.intents() == [("cli_stdin", "plain answer")]
        assert daemon.pipeline.channels == {body["turn_id"]: "cli_stdin"}

    _run(tmp_path, scenario)


def test_a_wait_line_is_not_the_answer_and_its_cancel_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"One moment." shares the turn; the ask waits for the final run and its written part."""
    monkeypatch.setattr(inherent_server, "_ASK_RECHECK_S", 0.05)

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        started = time.monotonic()
        response = await client.post("/inherent/ask", json={"text": "wait line then answer"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["spoken"], body["written"]) == (ANSWER, DETAILS)
        assert time.monotonic() - started >= 0.4  # it did not stop at the wait line
        assert daemon.submitted == ["wait line then answer"]

    _run(tmp_path, scenario)


def test_no_answer_in_time_is_a_504_with_the_turn_id_and_the_turn_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The question stays in the log under the id the 504 names."""
    monkeypatch.setattr(inherent_server, "_ASK_WAIT_S", 0.5)

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        started = time.monotonic()
        response = await client.post("/inherent/ask", json={"text": "never"})
        assert response.status_code == 504
        assert 0.5 <= time.monotonic() - started < 3
        body = response.json()
        assert set(body) == {"turn_id", "detail"}
        assert body["turn_id"]
        assert daemon.intents() == [("cli_stdin", "never")]
        assert daemon.pipeline.channels == {body["turn_id"]: "cli_stdin"}

    _run(tmp_path, scenario)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("run fails", "turn_raised"),
        ("run cancelled", "user_stop"),
        ("turn fails", "missing_key"),
    ],
)
def test_a_turn_that_fails_or_is_cancelled_is_a_502_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, reason: str,
) -> None:
    """`response.failed` never reaches the socket, so the short re-read is what finds that one."""
    monkeypatch.setattr(inherent_server, "_ASK_RECHECK_S", 0.05)

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        response = await client.post("/inherent/ask", json={"text": text})
        assert response.status_code == 502, response.text
        body = response.json()
        assert body["detail"] == reason
        assert body["turn_id"] in daemon.pipeline.channels

    _run(tmp_path, scenario)


def test_waiting_does_not_hold_the_event_loop(tmp_path: Path) -> None:
    """With an ask pending, other routes answer, and a second ask is answered meanwhile."""

    async def scenario(_daemon: _Daemon, client: httpx.AsyncClient) -> None:
        slow = asyncio.create_task(
            client.post("/inherent/ask", json={"text": "wait line then answer"}),
        )
        await asyncio.sleep(0.1)
        started = time.monotonic()
        assert (await client.get("/api/health")).json() == {"status": "ok"}
        quick = await client.post("/inherent/ask", json={"text": "plain answer"})
        assert quick.json()["spoken"] == ANSWER
        assert time.monotonic() - started < 0.35  # the slow one is still waiting for its answer
        assert not slow.done()
        assert (await slow).json()["written"] == DETAILS

    _run(tmp_path, scenario)


def test_the_body_is_checked_before_anything_is_submitted(tmp_path: Path) -> None:
    """Empty or missing text 400, over 2000 characters 400, over 4 KiB 413, nothing submitted."""

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        bodies: list[object] = [
            {"text": ""}, {"text": " \n "}, {}, {"text": 5}, {"text": "x" * 2001}, [],
        ]
        for body in bodies:
            response = await client.post("/inherent/ask", json=body)
            assert response.status_code == 400, body
        garbled = await client.post("/inherent/ask", content=b"{not json")
        assert garbled.status_code == 400
        assert "not json" not in garbled.text  # a body is never echoed back
        huge = await client.post("/inherent/ask", json={"text": "x", "pad": "y" * 5000})
        assert huge.status_code == 413
        assert daemon.submitted == []
        assert daemon.intents() == []

        edge = await client.post("/inherent/ask", json={"text": "x" * 2000})
        assert edge.status_code == 200
        assert edge.json()["spoken"] == "x" * 2000

    _run(tmp_path, scenario)


def test_it_takes_a_paired_devices_token_and_nothing_else_off_loopback(tmp_path: Path) -> None:
    """The guard in front of it is the one every other route has."""

    async def scenario(daemon: _Daemon, client: httpx.AsyncClient) -> None:
        no_token = await client.post(
            "/inherent/ask", json={"text": "plain answer"}, headers={"Authorization": ""},
        )
        assert no_token.status_code == 401
        local_key = await client.post(
            "/inherent/ask", json={"text": "plain answer"},
            headers={"Authorization": f"Bearer {LOCAL_KEY}"},
        )
        assert local_key.status_code == 401
        assert daemon.submitted == []
        ok = await client.post("/inherent/ask", json={"text": "plain answer"})
        assert ok.status_code == 200

    _run(tmp_path, scenario)


def test_a_daemon_that_cannot_read_turns_has_no_ask_route() -> None:
    """Unwired, the route does not exist, as every other optional route."""
    app = create_app(
        InherentDeps(submit_callable=lambda _t: None, broadcaster=InherentBroadcaster()),
    )
    assert "/inherent/ask" not in {getattr(route, "path", "") for route in app.routes}
