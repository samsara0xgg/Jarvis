"""A paired phone, a fake APNs and the sender over them."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, Any, Self

import h2.config
import h2.connection
import h2.events
import httpx
from cryptography.hazmat.primitives.asymmetric import ec

from jarvis.deployment import bootstrap_runtime
from jarvis.runtime.push import Push
from jarvis.state import reminders as folded
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.push_tokens import register_body
from jarvis.surface.apns import ApnsClient, ProviderToken

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime
    from pathlib import Path

KEY_ID = "ABC123DEFG"


TEAM_ID = "TEAM123456"


BUNDLE = "app.example.jarvis"


DEVICE_A = "a1" * 32


class FakeApns:
    """An HTTP/2 server on loopback that records each request and answers as told."""

    def __init__(self) -> None:
        """Not started; ``requests`` collects what arrives."""
        self.requests: list[dict[str, Any]] = []
        # ``(request) -> (status, reason)``; the default accepts everything.
        self.reply: Callable[[dict[str, Any]], tuple[int, str | None]] = lambda _r: (200, None)
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._up = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Self:
        """Start serving."""
        self._thread.start()
        assert self._up.wait(5)
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving, ending the connections the client kept open."""
        asyncio.run_coroutine_threadsafe(self._stop(), self._loop).result(5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        self._loop.close()

    async def _stop(self) -> None:
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def url(self) -> str:
        """The base URL the client is pointed at."""
        return f"http://127.0.0.1:{self.port}"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        server = self._loop.run_until_complete(asyncio.start_server(self._serve, "127.0.0.1", 0))
        self.port = server.sockets[0].getsockname()[1]
        self._up.set()
        self._loop.run_forever()
        server.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False))
        conn.initiate_connection()
        writer.write(conn.data_to_send())
        streams: dict[int, dict[str, Any]] = {}
        while data := await reader.read(65536):
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    headers = {k.decode(): v.decode() for k, v in event.headers}
                    streams[event.stream_id] = {"headers": headers, "body": b""}
                elif isinstance(event, h2.events.DataReceived):
                    streams[event.stream_id]["body"] += event.data
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    self._answer(conn, event.stream_id, streams.pop(event.stream_id))
            writer.write(conn.data_to_send())
            await writer.drain()
        writer.close()

    def _answer(self, conn: h2.connection.H2Connection, stream: int, got: dict[str, Any]) -> None:
        headers = got["headers"]
        request = {
            "path": headers[":path"],
            "device": headers[":path"].rsplit("/", 1)[-1],
            "headers": headers,
            "body": json.loads(got["body"]),
        }
        self.requests.append(request)
        status, reason = self.reply(request)
        body = b"" if reason is None else json.dumps({"reason": reason}).encode()
        conn.send_headers(stream, [(":status", str(status)), ("content-type", "application/json")])
        conn.send_data(stream, body, end_stream=True)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        """Start at a fixed instant."""
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        """Epoch seconds."""
        return self.now


class World:
    """A runtime root, a paired phone that registered, a fake APNs and the sender over them."""

    def __init__(self, root: Path, sandbox: FakeApns, production: FakeApns) -> None:
        """Build the sender over the two fake endpoints."""
        self.paths = bootstrap_runtime(root)
        self.root = self.paths.root
        self.sandbox, self.production = sandbox, production
        self.clock = Clock()
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.token = ProviderToken(self.key, KEY_ID, TEAM_ID, self.clock)
        self.client = ApnsClient(
            self.token,
            BUNDLE,
            transport=httpx.HTTPTransport(http1=False, http2=True),
            hosts={"sandbox": sandbox.url, "production": production.url},
            clock=self.clock,
        )
        self.push = Push(self.root, self.paths.event_log, self.client)
        self.push.clock = self.clock
        self.log = open_event_log(self.paths.event_log)
        self.phone_token = pair_device(self.root, "phone")

    def register(
        self,
        name: str = "phone",
        device: str = DEVICE_A,
        environment: str = "sandbox",
        live: str | None = None,
    ) -> None:
        """A device registers through the same function the route calls."""
        body: dict[str, Any] = {"device_token": device, "environment": environment}
        if live is not None:
            body["live_activity_token"] = live
        register_body(self.root, name, body)

    def requests(self) -> list[dict[str, Any]]:
        """Every request either endpoint got."""
        return [*self.sandbox.requests, *self.production.requests]

    def fired(self) -> int:
        """How many ``reminder.fired`` rows the log holds."""
        return sum(1 for _ in iter_events_of_types(self.log, ["reminder.fired"]))

    def remind(self, text: str, at: datetime) -> str:
        """Schedule a reminder."""
        return folded.schedule(self.log, due=at, text=text, action_id="A1")

    def confirm(
        self,
        cid: str,
        *,
        expires_in_s: float = 600,
        text: str = "Send the draft?",
        turn: str | None = None,
    ) -> None:
        """Ask for a confirmation, within ``turn`` if one is given."""
        emit_event(
            self.log,
            type="confirmation.requested",
            payload={
                "confirmation_id": cid,
                "action_snapshot": {"tool_name": "gmail_send"},
                "template_line": text,
                "expires_at_ms": int((self.clock() + expires_in_s) * 1000),
            },
            correlation=None if turn is None else {"turn_id": turn},
        )

    def ask(self, qid: str, question: str = "Which stop?", turn: str = "T1") -> None:
        """Put up an ask card."""
        emit_event(
            self.log,
            type="clarification.requested",
            payload={
                "clarification_id": qid,
                "question": question,
                "fields": [],
                "turn_id": turn,
            },
        )

    def turn(self, turn_id: str, device: str) -> None:
        """Open ``turn_id`` with words ``device`` typed (ADR 0212)."""
        emit_event(
            self.log,
            type="surface.user_intent",
            ingestion_node=device,
            payload={"transcript": "do it", "turn_id": turn_id, "channel": "cli_stdin"},
            correlation={"turn_id": turn_id},
        )

    def settle(self) -> None:
        """Wait for what the sender handed its own thread."""
        self.push._pool.submit(lambda: None).result(5)  # noqa: SLF001 — one worker, in order

    def close(self) -> None:
        """Release the client and the log."""
        self.client.close()
        self.log.close()
