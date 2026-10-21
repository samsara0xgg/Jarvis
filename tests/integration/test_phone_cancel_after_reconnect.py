"""A phone's stop reaches the turns it opened before its socket dropped (spec: the phone channel).

``cancel`` stops "this device's recent turns": the owner of a turn is the device named on its
opening row, not the socket that carried the words. Acceptance checks against a real uvicorn
server with the real ``/phone/ws`` route over a real event log, with the daemon's own turn-cancel
callable and open response runs registered the way a running answer registers them.

Shown: (a) a turn said on one socket is cancelled by a ``cancel`` on the next socket of the same
device, as ``user_stop`` in the log; (b) a dismissal verdict does the same; (c) another device's
open answer is left alone.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from typing import TYPE_CHECKING, Any, Self

import pytest
import uvicorn
from websockets.sync.client import connect as _connect

from jarvis.runtime.daemon import output_watchers
from jarvis.runtime.turn.cancel import make_turn_cancel_callable
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents, phone_turn_id
from tests.support.net import free_port
from tests.support.response_run import make_runtime, open_run
from tests.support.terminal import AsRemote

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision.response_run import ResponseRun
    from jarvis.runtime import JarvisRuntime

WAIT_S = 10.0


class _Server(uvicorn.Server):
    """A uvicorn server that says when it is listening."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.listening = threading.Event()

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets)
        self.listening.set()


class _Host:
    """The phone route over the daemon's real turn-cancel callable.

    ``stopped`` fires after each cancel the route asks for.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.runtime: JarvisRuntime = make_runtime(tmp_path / "runtime")
        keys = tmp_path / "keys"
        keys.mkdir()
        self.tokens = {name: pair_device(keys, name) for name in ("iphone", "ipad")}
        self.cancel_calls: list[tuple[str, str]] = []
        self.stopped = threading.Event()
        cancel = make_turn_cancel_callable(self.runtime)

        def cancel_turn(turn_id: str, reason: str) -> str:
            outcome = cancel(turn_id, reason)
            self.cancel_calls.append((turn_id, reason))
            self.stopped.set()
            return outcome

        self._log = sqlite3.connect(self.runtime.runtime_paths.event_log, check_same_thread=False)
        hub = PhoneHub(
            events=BrainEvents(self._log), rows=output_watchers.PhoneRows(self._log),
            cancel_turn=cancel_turn,
        )
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T1", broadcaster=InherentBroadcaster(), phone=hub,
            device_name=lambda one: device_name_for_token(keys, one),
        ))
        require_local_key(
            app, lambda header: local_key_matches(local_key(keys), header),
            device_token_matches=lambda one: device_token_matches(keys, one),
        )
        self.port = free_port()
        self.server = _Server(uvicorn.Config(
            AsRemote(app), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off",
        ))
        self.thread = threading.Thread(target=lambda: asyncio.run(self.server.serve()), daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        assert self.server.listening.wait(WAIT_S)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=WAIT_S)
        self._log.close()
        self.runtime.conn.close()

    def connect(self, device: str) -> Any:  # noqa: ANN401 - the websockets client connection
        """A socket of ``device`` that has said hello and been told it is ready."""
        ws = _connect(
            f"ws://127.0.0.1:{self.port}{PHONE_PATH}", proxy=None,
            additional_headers={"Authorization": f"Bearer {self.tokens[device]}"},
        )
        ws.send(json.dumps({"type": "hello", "voice": False}))
        assert json.loads(ws.recv(WAIT_S))["type"] == "ready"
        return ws

    def cancelled(self) -> list[tuple[str, str]]:
        """``(response id, reason)`` of every ``response.cancelled`` row, from the log."""
        return [
            (str(response_id), str(reason)) for response_id, reason in self.runtime.conn.execute(
                "SELECT json_extract(payload_json, '$.response_id'), "
                "json_extract(payload_json, '$.reason') FROM events "
                "WHERE type = 'response.cancelled' ORDER BY id",
            )
        ]


@pytest.fixture
def host(tmp_path: Path) -> Iterator[_Host]:
    """One server with two paired devices, stopped after the test."""
    with _Host(tmp_path) as one:
        yield one


def _say(
    ws: Any, utterance_id: str, text: str, *, spoken: bool = False, **flags: Any,  # noqa: ANN401
) -> dict[str, Any]:
    ws.send(json.dumps({
        "type": "say", "utterance_id": utterance_id, "text": text, "spoken": spoken, **flags,
    }))
    while (reply := json.loads(ws.recv(WAIT_S)))["type"] not in {"said", "error"}:
        pass
    return dict(reply)


def _answering(host: _Host, device: str, utterance_id: str) -> tuple[str, ResponseRun]:
    """``device`` says something on a socket that then drops; its answer is under way."""
    ws = host.connect(device)
    said = _say(ws, utterance_id, "写一封很长的信")
    ws.close()
    turn_id = said["turn_id"]
    assert turn_id == phone_turn_id(device, utterance_id)
    return turn_id, open_run(host.runtime, response_id=f"RESP-{device}", turn_id=turn_id)


def test_a_cancel_on_a_new_socket_stops_the_answer_the_old_socket_asked_for(host: _Host) -> None:
    """The stop button after a reconnect cancels the generation; another device's is left alone."""
    turn_id, run = _answering(host, "iphone", "u1")
    _other_turn, other = _answering(host, "ipad", "u9")
    ws = host.connect("iphone")  # the first socket is gone; this one opened nothing
    ws.send(json.dumps({"type": "cancel"}))
    assert host.stopped.wait(WAIT_S)
    ws.close()
    assert host.cancel_calls == [(turn_id, "user_stop")]
    assert host.cancelled() == [("RESP-iphone", "user_stop")]
    assert run.cancellation_token.is_cancelled
    assert not other.cancellation_token.is_cancelled


def test_a_dismissal_on_a_new_socket_stops_the_answer_the_old_socket_asked_for(host: _Host) -> None:
    """Words that dismiss her do what cancel does, whichever socket asked for the answer."""
    turn_id, run = _answering(host, "iphone", "u1")
    _other_turn, other = _answering(host, "ipad", "u9")
    ws = host.connect("iphone")
    reply = _say(ws, "u2", "退下", spoken=True, conversation=True)
    ws.close()
    assert (reply["verdict"], reply["turn_id"]) == ("dismissed", None)
    assert host.cancel_calls == [(turn_id, "user_stop")]
    assert host.cancelled() == [("RESP-iphone", "user_stop")]
    assert run.cancellation_token.is_cancelled
    assert not other.cancellation_token.is_cancelled
