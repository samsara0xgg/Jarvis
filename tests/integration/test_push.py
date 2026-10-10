"""ADR 0210: the host pushes to the paired iPhone through APNs.

Acceptance checks against the real code, a real event log and an in-process fake APNs that speaks
HTTP/2 (prior knowledge, on loopback) with a throwaway EC key generated here:

- registration: a paired device's token stores the APNs tokens, the local key and strangers are
  refused, a bad body is a 400 that echoes nothing, unpair removes the row;
- a fired reminder sends exactly one push with the right payload at any quiet level, and a push
  that fails or raises never makes the reminder ring again;
- a waiting confirmation or ask card pushes once, not while a phone socket is open, as the quiet
  level allows; a Claude Code permission request pushes once;
- the generic ``send`` carries a URL, a category and a thread;
- a 410 drops the token it came back for; a refusal is never retried; the endpoint follows the
  device's environment;
- the provider token has the right header and claims, verifies, and is reused under 60 minutes;
- the Live Activity gets its content state when it changed, and only then;
- with no key the host is off and says so once; and no log line holds a token, a JWT or the text.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

import h2.config
import h2.connection
import h2.events
import httpx
import pytest
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from jarvis.deployment import bootstrap_runtime
from jarvis.runtime import inherent_loop
from jarvis.runtime.push import KEY_ENV, Push
from jarvis.runtime.reminders import SPEAK_UNTIL, Reminders, line
from jarvis.shared import lang
from jarvis.state import push_tokens
from jarvis.state import reminders as folded
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
    unpair_device,
)
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.push_tokens import register_body
from jarvis.surface.apns import (
    REGISTER_PATH,
    TOKEN_TTL_S,
    ApnsClient,
    ProviderToken,
    PushMessage,
    alert_payload,
)
from jarvis.surface.claude_hooks import ClaudeHooks
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents
from tests.integration.test_terminal_voice import _AsRemote, _free_port, _wait_for
from tools.phone_fake_client import FakePhone

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

REMOTE = "100.87.250.92"
KEY_ID = "ABC123DEFG"
TEAM_ID = "TEAM123456"
BUNDLE = "app.example.jarvis"
DEVICE_A = "a1" * 32
DEVICE_B = "b2" * 32
LIVE_A = "c3" * 60
UBER = "uber://?action=setPickup&pickup=my_location&dropoff[formatted_address]=UVic"


# --- the fake APNs ---------------------------------------------------------------------------


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


# --- one runtime root with a paired phone ----------------------------------------------------


def _pem() -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


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
        self, name: str = "phone", device: str = DEVICE_A, environment: str = "sandbox",
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
        self, cid: str, *, expires_in_s: float = 600, text: str = "Send the draft?",
        turn: str | None = None,
    ) -> None:
        """Ask for a confirmation, within ``turn`` if one is given."""
        emit_event(self.log, type="confirmation.requested", payload={
            "confirmation_id": cid, "action_snapshot": {"tool_name": "gmail_send"},
            "template_line": text, "expires_at_ms": int((self.clock() + expires_in_s) * 1000),
        }, correlation=None if turn is None else {"turn_id": turn})

    def ask(self, qid: str, question: str = "Which stop?", turn: str = "T1") -> None:
        """Put up an ask card."""
        emit_event(self.log, type="clarification.requested", payload={
            "clarification_id": qid, "question": question, "fields": [], "turn_id": turn,
        })

    def turn(self, turn_id: str, device: str) -> None:
        """Open ``turn_id`` with words ``device`` typed (ADR 0212)."""
        emit_event(
            self.log, type="surface.user_intent", ingestion_node=device,
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


@pytest.fixture
def world(tmp_path: Path) -> Any:  # noqa: ANN401 — a generator of World.
    """A runtime root with a paired phone that registered a sandbox device token."""
    with FakeApns() as sandbox, FakeApns() as production:
        one = World(tmp_path, sandbox, production)
        one.register()
        yield one
        one.close()


def _answering(status: int, reason: str) -> Callable[[dict[str, Any]], tuple[int, str | None]]:
    """A reply function that always answers ``status`` with ``reason``."""
    return lambda _request: (status, reason)


def _b64(part: str) -> bytes:
    import base64  # noqa: PLC0415

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _bearer(request: dict[str, Any]) -> str:
    scheme, _, jwt = request["headers"]["authorization"].partition(" ")
    assert scheme == "bearer"
    return str(jwt)


# --- the provider token ----------------------------------------------------------------------


def test_the_provider_token_has_the_apns_header_and_claims_verifies_and_is_reused_under_an_hour(
    world: World,
) -> None:
    """The provider token has the apns header and claims verifies and is reused under an hour."""
    first = world.token.current()
    head, claims, signature = first.split(".")
    assert json.loads(_b64(head)) == {"alg": "ES256", "kid": KEY_ID}
    assert json.loads(_b64(claims)) == {"iss": TEAM_ID, "iat": int(world.clock())}
    raw = _b64(signature)
    assert len(raw) == 64  # r and s as 32 bytes each, not DER
    world.key.public_key().verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )

    world.clock.now += TOKEN_TTL_S - 1
    assert world.token.current() == first  # reused: APNs refuses a token replaced within 20 minutes
    world.clock.now += 1
    second = world.token.current()
    assert second != first
    assert json.loads(_b64(second.split(".")[1]))["iat"] == int(world.clock())
    assert TOKEN_TTL_S < 60 * 60  # refreshed well before APNs would refuse it


def test_every_request_carries_the_held_token_until_it_is_refreshed(world: World) -> None:
    """Every request carries the held token until it is refreshed."""
    world.push.send("t", "one")
    world.push.send("t", "two")
    world.clock.now += TOKEN_TTL_S
    world.push.send("t", "three")
    tokens = [_bearer(r) for r in world.requests()]
    assert tokens[0] == tokens[1] != tokens[2]


def test_a_refused_provider_token_is_dropped_and_the_next_push_signs_a_new_one(
    world: World,
) -> None:
    """A refused provider token is dropped and the next push signs a new one."""
    world.sandbox.reply = lambda _r: (
        (403, "ExpiredProviderToken") if len(world.requests()) == 1 else (200, None)
    )
    assert world.push.send("t", "one") == 0
    assert world.push.send("t", "two") == 1
    first, second = (_bearer(r) for r in world.requests())
    assert first != second
    assert len(world.requests()) == 2  # the refusal was not retried inside the first call


# --- registration ----------------------------------------------------------------------------


def _client(root: Path, *, listens: bool = True, remote: bool = True) -> TestClient:
    app = create_app(InherentDeps(
        submit_callable=lambda _text: "T1",
        broadcaster=InherentBroadcaster(),
        device_name=functools.partial(device_name_for_token, root),
        push_register=functools.partial(register_body, root) if listens else None,
    ))
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(root)),
        device_token_matches=functools.partial(device_token_matches, root),
    )
    return TestClient(
        app, base_url="http://127.0.0.1:8006", client=(REMOTE if remote else "127.0.0.1", 50000),
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_paired_phone_registers_its_tokens_and_unpair_removes_them(tmp_path: Path) -> None:
    """A paired phone registers its tokens and unpair removes them."""
    token = pair_device(tmp_path, "phone")
    client = _client(tmp_path)
    sent = {
        "device_token": DEVICE_A.upper(), "environment": "sandbox", "live_activity_token": LIVE_A,
    }
    reply = client.post(REGISTER_PATH, json=sent, headers=_auth(token))
    assert (reply.status_code, reply.json()) == (200, {"ok": True})
    assert push_tokens.registrations(tmp_path) == {
        "phone": push_tokens.Registration(DEVICE_A, "sandbox", LIVE_A),  # lowered
    }
    assert stat.S_IMODE((tmp_path / "push-tokens.json").stat().st_mode) == 0o600

    # a new device token without the live key leaves the live token; null clears it
    again = {"device_token": DEVICE_B, "environment": "production"}
    assert client.post(REGISTER_PATH, json=again, headers=_auth(token)).status_code == 200
    assert push_tokens.registrations(tmp_path)["phone"] == push_tokens.Registration(
        DEVICE_B, "production", None,
    )  # a changed environment makes the old live token meaningless
    client.post(REGISTER_PATH, json={**sent, "environment": "production"}, headers=_auth(token))
    client.post(REGISTER_PATH, json={**again, "device_token": DEVICE_A}, headers=_auth(token))
    assert push_tokens.registrations(tmp_path)["phone"].live_activity_token == LIVE_A
    client.post(
        REGISTER_PATH, json={**again, "live_activity_token": None}, headers=_auth(token),
    )
    assert push_tokens.registrations(tmp_path)["phone"].live_activity_token is None

    other = pair_device(tmp_path, "ipad")
    client.post(REGISTER_PATH, json=again, headers=_auth(other))
    assert set(push_tokens.registrations(tmp_path)) == {"phone", "ipad"}

    unpair_device(tmp_path, "phone")
    assert set(push_tokens.registrations(tmp_path)) == {"ipad"}
    assert client.post(REGISTER_PATH, json=again, headers=_auth(token)).status_code == 401


def test_the_local_key_and_strangers_are_refused_and_a_daemon_that_does_not_listen_has_no_route(
    tmp_path: Path,
) -> None:
    """The local key and strangers are refused and a daemon that does not listen has no route."""
    token = pair_device(tmp_path, "phone")
    body = {"device_token": DEVICE_A, "environment": "sandbox"}
    local = _client(tmp_path, remote=False)
    keyed = local.post(REGISTER_PATH, json=body, headers=_auth(local_key(tmp_path)))
    assert (keyed.status_code, keyed.json()) == (
        403, {"detail": "a paired device's token is required"},
    )
    assert local.post(REGISTER_PATH, json=body).status_code == 401
    # a device token is not the local key: on loopback the guard checks the key
    assert local.post(REGISTER_PATH, json=body, headers=_auth(token)).status_code == 401

    remote = _client(tmp_path)
    assert remote.post(REGISTER_PATH, json=body).status_code == 401
    assert remote.post(REGISTER_PATH, json=body, headers=_auth("wrong")).status_code == 401
    keyed_remote = remote.post(REGISTER_PATH, json=body, headers=_auth(local_key(tmp_path)))
    assert keyed_remote.status_code == 401
    assert push_tokens.registrations(tmp_path) == {}

    assert _client(tmp_path, listens=False).post(
        REGISTER_PATH, json=body, headers=_auth(token),
    ).status_code == 404


@pytest.mark.parametrize("body", [
    [],
    {"environment": "sandbox"},
    {"device_token": DEVICE_A},
    {"device_token": DEVICE_A, "environment": "staging"},
    {"device_token": "zz" * 32, "environment": "sandbox"},
    {"device_token": "ab", "environment": "sandbox"},
    {"device_token": 12, "environment": "sandbox"},
    {"device_token": DEVICE_A, "environment": "sandbox", "live_activity_token": "nothex"},
    {"device_token": DEVICE_A, "environment": "sandbox", "name": "other-phone"},
])
def test_a_registration_that_is_not_exactly_the_shape_is_a_400_that_echoes_nothing(
    tmp_path: Path, body: object,
) -> None:
    """A registration that is not exactly the shape is a 400 that echoes nothing."""
    token = pair_device(tmp_path, "phone")
    reply = _client(tmp_path).post(REGISTER_PATH, json=body, headers=_auth(token))
    assert reply.status_code == 400
    assert "other-phone" not in reply.text
    assert "zzzz" not in reply.text
    assert push_tokens.registrations(tmp_path) == {}


def test_an_oversize_or_malformed_body_is_refused(tmp_path: Path) -> None:
    """An oversize or malformed body is refused."""
    token = pair_device(tmp_path, "phone")
    client = _client(tmp_path)
    big = client.post(
        REGISTER_PATH, content=b"{" + b" " * 5000 + b"}",
        headers={**_auth(token), "content-type": "application/json"},
    )
    assert big.status_code == 413
    junk = client.post(
        REGISTER_PATH, content=b"not json",
        headers={**_auth(token), "content-type": "application/json"},
    )
    assert junk.status_code == 400


# --- reminders -------------------------------------------------------------------------------


def _reminders(world: World) -> Reminders:
    reminders = Reminders(world.paths.event_log)
    reminders.now = lambda: datetime.fromtimestamp(world.clock(), UTC)
    reminders.push = world.push.reminder
    return reminders


def _due(world: World, seconds_ago: float = 0) -> datetime:
    return datetime.fromtimestamp(world.clock() - seconds_ago, UTC)


def test_a_fired_reminder_sends_exactly_one_push_with_the_right_payload_at_any_quiet_level(
    world: World,
) -> None:
    """A fired reminder sends exactly one push with the right payload at any quiet level."""
    world.push.quiet = lambda: "dnd"  # a reminder ignores it, as it does everywhere (ADR 0179)
    reminders = _reminders(world)
    world.remind("call the landlord", _due(world, 30))

    assert reminders.tick() == 1
    assert reminders.tick() == 0

    [request] = world.requests()
    assert request["path"] == f"/3/device/{DEVICE_A}"
    assert {k: request["headers"][k] for k in (
        "apns-push-type", "apns-topic", "apns-priority", "content-type",
    )} == {
        "apns-push-type": "alert", "apns-topic": BUNDLE, "apns-priority": "10",
        "content-type": "application/json",
    }
    assert int(request["headers"]["apns-expiration"]) == int(world.clock()) + 3600
    assert request["body"] == {"aps": {
        "alert": {"title": "Jarvis", "body": line("call the landlord", 30_000)},
        "sound": "default", "interruption-level": "time-sensitive",
        "thread-id": "reminders", "category": "reminder",
    }}
    assert world.fired() == 1


def test_a_push_that_fails_or_raises_never_rings_the_reminder_again(world: World) -> None:
    """A push that fails or raises never rings the reminder again."""
    world.sandbox.reply = lambda _r: (500, "InternalServerError")
    reminders = _reminders(world)
    world.remind("first", _due(world))
    assert reminders.tick() == 1
    assert reminders.tick() == 0
    assert (len(world.requests()), world.fired()) == (1, 1)  # one try, no loop, no second ring

    def broken(_line: str) -> None:
        raise RuntimeError

    reminders.push = broken
    world.remind("second", _due(world))
    assert reminders.tick() == 1
    assert reminders.tick() == 0
    assert world.fired() == 2


def test_a_reminder_hours_late_is_not_pushed(world: World) -> None:
    """A reminder hours late is not pushed."""
    reminders = _reminders(world)
    world.remind("old", _due(world, (SPEAK_UNTIL + timedelta(minutes=1)).total_seconds()))
    assert reminders.tick() == 1
    assert world.requests() == []
    assert world.fired() == 1


# --- waiting items ---------------------------------------------------------------------------


def test_a_waiting_confirmation_pushes_once_and_what_was_waiting_at_boot_is_not_news(
    world: World,
) -> None:
    """A waiting confirmation pushes once and what was waiting at boot is not news."""
    world.confirm("C0")  # waiting before the watcher started
    world.push.poll()
    assert world.requests() == []

    world.confirm("C1", text="Send the draft to Dana?")
    world.push.poll()
    world.push.poll()
    [request] = world.requests()
    assert request["body"] == {"aps": {
        "alert": {"title": lang.t("push.confirmation"), "body": "Send the draft to Dana?"},
        "sound": "default", "thread-id": "waiting", "category": "confirmation",
    }}
    assert request["headers"]["apns-priority"] == "10"


def test_no_waiting_push_goes_out_while_a_phone_socket_is_open(world: World) -> None:
    """No waiting push goes out while a phone socket is open."""
    world.push.poll()
    world.push.phone_socket_open = lambda _device: True
    world.confirm("C1")
    world.ask("Q1")
    world.push.poll()
    assert world.requests() == []

    # seen while the socket was open: closing it later does not push them after the fact
    world.push.phone_socket_open = lambda _device: False
    world.push.poll()
    assert world.requests() == []
    world.confirm("C2")
    world.push.poll()
    assert [r["body"]["aps"]["category"] for r in world.requests()] == ["confirmation"]


def test_waiting_pushes_follow_the_quiet_level(world: World) -> None:
    """Waiting pushes follow the quiet level."""
    world.push.poll()
    for n, level in enumerate(("off", "quiet", "no-pop", "dnd")):
        world.push.quiet = lambda: level  # noqa: B023 — polled before the next turn
        world.confirm(f"C{n}")
        world.push.poll()
    sounds = [("sound" in r["body"]["aps"]) for r in world.requests()]
    assert sounds == [True, False]  # sound at off, a silent banner at quiet, nothing from no-pop up


def test_an_ask_card_pushes_and_a_card_closed_or_expired_before_the_poll_does_not(
    world: World,
) -> None:
    """An ask card pushes and a card closed or expired before the poll does not."""
    world.push.poll()
    world.ask("Q1", "Which stop?")
    world.ask("Q2", "Closed already?")
    emit_event(
        world.log, type="surface.dismissed", payload={"turn_id": "T1", "clarification_id": "Q2"},
    )
    world.confirm("C1", text="Answered already?")
    emit_event(world.log, type="confirmation.rejected", payload={
        "confirmation_id": "C1", "utterance_raw": "no", "grammar_rule_id": "r",
    })
    world.confirm("C2", expires_in_s=-5, text="Expired already?")
    world.push.poll()
    [request] = world.requests()
    assert request["body"]["aps"]["alert"] == {
        "title": lang.t("push.question"), "body": "Which stop?",
    }
    assert request["body"]["aps"]["category"] == "question"


def test_a_claude_code_permission_request_pushes_once(world: World) -> None:
    """A claude code permission request pushes once."""
    async def hold() -> None:
        hooks = ClaudeHooks(lambda: "off", world.push.claude_request)
        hooks.merge({"sessions": []})  # a companion is reading the board
        async def gone() -> bool:
            return False
        task = asyncio.create_task(hooks.permission(
            {"session_id": "S1", "tool_name": "Bash", "cwd": "/Users/a/Jarvis",
             "tool_input": {"command": "rm -rf /secret"}}, gone,
        ))
        for _ in range(250):  # the push goes off the loop, on the sender's own thread
            if world.requests():
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)  # long enough for a second push, which must not come
        task.cancel()

    asyncio.run(hold())
    [request] = world.requests()
    assert request["body"]["aps"]["alert"] == {
        "title": lang.t("push.claude"), "body": "Bash · Jarvis",  # never the command
    }
    assert request["body"]["aps"]["category"] == "claude"


class PhoneHost:
    """A real uvicorn server with the real ``/phone/ws`` route over the world's event log."""

    def __init__(self, world: World) -> None:
        """Serve the route for the world's paired phone; no voice, the host speaks nothing."""
        self.world = world
        self.port = _free_port()
        self.hub: PhoneHub | None = None
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        conn = sqlite3.connect(self.world.paths.event_log, check_same_thread=False)
        self.hub = PhoneHub(
            events=BrainEvents(conn), rows=inherent_loop._PhoneRows(conn),  # noqa: SLF001
        )
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            phone=self.hub,
            device_name=functools.partial(device_name_for_token, self.world.root),
        ))
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.world.root)),
            device_token_matches=functools.partial(device_token_matches, self.world.root),
        )
        self.server = uvicorn.Server(uvicorn.Config(
            _AsRemote(app), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off",
        ))
        await self.server.serve()

    def __enter__(self) -> Self:
        """Start serving and wait until it is up."""
        self.thread.start()
        _wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving."""
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def phone(self) -> FakePhone:
        """A text-only phone that is paired as ``phone``."""
        url = f"ws://127.0.0.1:{self.port}{PHONE_PATH}"
        return FakePhone(url, self.world.phone_token, voice=False).start()


def test_a_waiting_item_is_not_pushed_while_the_phones_real_socket_is_open_and_is_after_it_closes(
    world: World,
) -> None:
    """The flag is the host's own record of ``/phone/ws`` connections, per device (ADR 0209)."""
    with PhoneHost(world) as host:
        assert host.hub is not None
        hub = host.hub
        world.push.phone_socket_open = hub.connected  # as serve_inherent wires it
        world.push.poll()
        assert not hub.connected("phone")
        world.confirm("C0", text="Nobody is connected")
        world.push.poll()
        assert [r["body"]["aps"]["alert"]["body"] for r in world.requests()] == [
            "Nobody is connected",
        ]

        phone = host.phone()
        try:
            assert phone.hello()["device"] == "phone"
            assert hub.connected("phone")
            world.confirm("C1", text="The app is open")
            world.ask("Q1", "Also open?")
            world.push.poll()
            assert len(world.requests()) == 1  # nothing for the two items asked while it is open
        finally:
            phone.close()
        _wait_for(lambda: not hub.connected("phone"), "the socket never closed")

        world.confirm("C2", text="The app was closed")
        world.push.poll()
        assert [r["body"]["aps"]["alert"]["body"] for r in world.requests()] == [
            "Nobody is connected", "The app was closed",
        ]


def test_only_the_device_with_the_open_socket_is_skipped(world: World) -> None:
    """A second paired device with no socket still gets the push."""
    world.register("ipad", DEVICE_B, "production")
    world.push.phone_socket_open = lambda device: device == "phone"
    world.push.poll()
    world.confirm("C1")
    world.push.poll()
    assert [r["device"] for r in world.requests()] == [DEVICE_B]
    assert world.production.requests
    assert not world.sandbox.requests


def test_a_card_goes_only_to_the_phone_whose_turn_asked_it_and_a_computers_never(
    world: World,
) -> None:
    """ADR 0218: the asking turn's device decides; whether he is at the Mac does not."""
    world.register("ipad", DEVICE_B, "production")
    world.push.at_mac = lambda: True
    world.turn("T1", "phone")
    world.turn("T2", "macbook")
    world.push.poll()
    world.confirm("C1", turn="T1", text="From the phone")
    world.ask("Q1", "From the Mac?", turn="T2")
    world.confirm("C2", turn="T2", text="Also from the Mac")
    world.push.poll()
    assert [(r["device"], r["body"]["aps"]["alert"]["body"]) for r in world.requests()] == [
        (DEVICE_A, "From the phone"),
    ]


def test_a_card_no_turn_asked_is_pushed_only_while_he_is_away_from_the_mac(
    world: World,
) -> None:
    """ADR 0218: with no device it goes where he is when it is asked; unknown counts as away."""
    world.push.poll()
    world.push.at_mac = lambda: True
    world.confirm("C1", text="Asked at the Mac")
    world.push.poll()
    world.push.at_mac = lambda: False
    world.push.poll()  # not asked again: a card no turn asked is not followed
    world.confirm("C2", text="Asked while away")
    world.push.poll()
    assert [r["body"]["aps"]["alert"]["body"] for r in world.requests()] == ["Asked while away"]


def test_a_held_claude_request_waits_while_he_is_at_the_mac_and_follows_him_off_it(
    world: World,
) -> None:
    """ADR 0218: pushed once he leaves while it still waits; one answered first never is."""
    at_mac = [True]
    world.push.at_mac = lambda: at_mac[0]
    world.push.poll()
    waiting = {"R1": True, "R2": True}
    world.push.claude_request("Bash", "/Users/a/Projects", "R1", lambda: waiting["R1"])
    world.push.claude_request("Edit", "/Users/a/Projects", "R2", lambda: waiting["R2"])
    world.settle()
    world.push.poll()
    assert world.requests() == []  # he is at the Mac: the notch has it

    waiting["R2"] = False  # answered on the notch, or in Claude's app
    at_mac[0] = False
    world.push.poll()
    world.push.poll()
    [request] = world.requests()
    assert request["body"]["aps"]["alert"] == {
        "title": lang.t("push.claude"), "body": "Bash · Projects",
    }


def test_a_claude_request_held_while_he_is_away_pushes_at_once(world: World) -> None:
    """Away from the Mac, the hold itself pushes, as before (ADR 0210), without a poll."""
    world.push.claude_request("Bash", "/Users/a/Jarvis", "R1", lambda: True)
    world.settle()
    assert [r["body"]["aps"]["alert"]["body"] for r in world.requests()] == ["Bash · Jarvis"]
    world.push.poll()
    world.push.poll()
    assert len(world.requests()) == 1


# --- the generic send ------------------------------------------------------------------------


def test_send_carries_the_url_the_category_and_the_thread(world: World) -> None:
    """Send carries the url the category and the thread."""
    assert world.push.send(
        "Ride to UVic", "Leaves in 10 minutes", url=UBER, category="transit", thread="trip",
    ) == 1
    [request] = world.requests()
    assert request["body"] == {
        "aps": {
            "alert": {"title": "Ride to UVic", "body": "Leaves in 10 minutes"},
            "sound": "default", "thread-id": "trip", "category": "transit",
        },
        "url": UBER,
    }
    assert world.push.send("plain", "no extras") == 1
    assert world.requests()[-1]["body"] == {
        "aps": {"alert": {"title": "plain", "body": "no extras"}, "sound": "default"},
    }


@pytest.mark.parametrize("bad", [
    {"url": "no scheme"}, {"url": "x:" + "a" * 2000}, {"category": ""}, {"thread": "t" * 65},
])
def test_a_message_that_cannot_be_a_push_is_the_callers_error_and_sends_nothing(
    world: World, bad: dict[str, str],
) -> None:
    """A message that cannot be a push is the callers error and sends nothing."""
    with pytest.raises(ValueError, match=r"url|category|thread"):
        world.push.send("t", "b", **bad)
    assert world.requests() == []


def test_a_long_message_stays_under_the_apns_limit() -> None:
    """A long message stays under the apns limit."""
    body = alert_payload(PushMessage("题" * 500, "文" * 5000, url="https://e.test/" + "p" * 900,
                                     category="c" * 64, thread="t" * 64))
    assert len(body) <= 4096


# --- failure and endpoints -------------------------------------------------------------------


def test_a_410_drops_the_token_it_came_back_for(world: World) -> None:
    """A 410 drops the token it came back for."""
    world.register("tablet", DEVICE_B, "sandbox")
    world.sandbox.reply = lambda r: (
        (410, "Unregistered") if r["device"] == DEVICE_B else (200, None)
    )
    assert world.push.send("t", "b") == 1
    assert set(push_tokens.registrations(world.root)) == {"phone"}
    assert world.push.send("t", "again") == 1
    assert [r["device"] for r in world.requests()] == [DEVICE_A, DEVICE_B, DEVICE_A]


def test_a_410_for_the_live_activity_drops_only_that_token(world: World) -> None:
    """A 410 for the live activity drops only that token."""
    world.register(live=LIVE_A)
    world.sandbox.reply = lambda r: (410, "Unregistered") if r["device"] == LIVE_A else (200, None)
    world.push.poll()
    assert push_tokens.registrations(world.root)["phone"] == push_tokens.Registration(
        DEVICE_A, "sandbox", None,
    )


@pytest.mark.parametrize(("status", "reason"), [
    (400, "BadDeviceToken"), (403, "InvalidProviderToken"), (429, "TooManyRequests"),
    (500, "InternalServerError"), (503, "ServiceUnavailable"),
])
def test_any_other_refusal_keeps_the_token_and_is_tried_once(
    world: World, status: int, reason: str,
) -> None:
    """Any other refusal keeps the token and is tried once."""
    world.sandbox.reply = lambda _r: (status, reason)
    assert world.push.send("t", "b") == 0
    assert len(world.requests()) == 1
    assert set(push_tokens.registrations(world.root)) == {"phone"}


def test_an_unreachable_apns_is_one_logged_failure_not_an_error_for_the_caller(
    world: World,
) -> None:
    """An unreachable apns is one logged failure not an error for the caller."""
    world.client._hosts = {"sandbox": "http://127.0.0.1:1", "production": "x"}  # noqa: SLF001
    assert world.push.send("t", "b") == 0


def test_each_device_is_pushed_at_the_endpoint_it_registered_with(world: World) -> None:
    """Each device is pushed at the endpoint it registered with."""
    world.register("ipad", DEVICE_B, "production")
    assert world.push.send("t", "b") == 2
    assert [r["device"] for r in world.sandbox.requests] == [DEVICE_A]
    assert [r["device"] for r in world.production.requests] == [DEVICE_B]


# --- the Live Activity -----------------------------------------------------------------------


def test_the_live_activity_gets_its_content_state_when_it_changed_and_only_then(
    world: World,
) -> None:
    """The live activity gets its content state when it changed and only then."""
    world.register(live=LIVE_A)
    reminders = _reminders(world)
    world.push.next_reminder = reminders.next_due
    state = {"value": "idle"}
    world.push.her_state = lambda: state["value"]
    due = _due(world, -3600)
    world.remind("dentist", due)

    world.push.poll()
    world.push.poll()
    [update] = world.requests()
    assert update["path"] == f"/3/device/{LIVE_A}"
    assert update["headers"]["apns-push-type"] == "liveactivity"
    assert update["headers"]["apns-topic"] == f"{BUNDLE}.push-type.liveactivity"
    assert update["body"] == {"aps": {
        "timestamp": int(world.clock()), "event": "update",
        "content-state": {
            "state": "idle", "next_text": "dentist", "next_at_ms": int(due.timestamp() * 1000),
        },
    }}

    state["value"] = "conversation"
    world.push.poll()
    world.clock.now += 3601
    assert reminders.tick() == 1  # the reminder fired: nothing is next
    world.push.poll()
    states = [r["body"]["aps"].get("content-state") for r in world.requests()
              if r["device"] == LIVE_A]
    assert [s["state"] for s in states] == ["idle", "conversation", "conversation"]
    assert states[-1]["next_text"] is None
    assert states[-1]["next_at_ms"] is None


def test_without_a_live_activity_token_nothing_is_sent_for_it(world: World) -> None:
    """Without a live activity token nothing is sent for it."""
    world.push.poll()
    assert world.requests() == []


# --- no key, and what is never logged --------------------------------------------------------


def _config(**block: str) -> dict[str, Any]:
    return {"push": {"key_id": KEY_ID, "team_id": TEAM_ID, "bundle_id": BUNDLE, **block}}


def _boot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, config: dict[str, Any],
) -> tuple[Push, list[logging.LogRecord]]:
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="jarvis.runtime.push"):
        push = Push.from_config(config, tmp_path, tmp_path / "events.db")
    return push, [r for r in caplog.records if r.name == "jarvis.runtime.push"]


def test_without_a_key_the_host_is_off_and_says_so_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a key the host is off and says so once."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    configs: list[dict[str, Any]] = [
        {}, {"push": {}}, _config(key_file=""), {"push": {"key_file": "none.p8"}},
    ]
    for config in configs:
        push, records = _boot(tmp_path, caplog, config)
        assert (push.enabled, len(records)) == (False, 1)
        assert push.send("t", "b") == 0
        assert push.reminder("r") == 0
        assert push.waiting("question", "Q", "text") is False


def test_a_key_file_must_be_a_private_file_under_the_runtime_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key file must be a private file under the runtime root."""
    monkeypatch.delenv(KEY_ENV, raising=False)
    pem = _pem()
    key = tmp_path / "AuthKey.p8"
    key.write_bytes(pem)
    key.chmod(0o644)
    push, records = _boot(tmp_path, caplog, _config(key_file="AuthKey.p8"))
    assert (push.enabled, len(records)) == (False, 1)
    assert "0600" in records[0].getMessage()

    key.chmod(0o600)
    push, records = _boot(tmp_path, caplog, _config(key_file="AuthKey.p8"))
    assert (push.enabled, len(records)) == (True, 1)

    outside = tmp_path.parent / f"{tmp_path.name}-outside.p8"
    outside.write_bytes(pem)
    outside.chmod(0o600)
    push, records = _boot(tmp_path, caplog, _config(key_file=str(outside)))
    assert (push.enabled, len(records)) == (False, 1)

    (tmp_path / "link.p8").symlink_to(outside)
    push, records = _boot(tmp_path, caplog, _config(key_file="link.p8"))
    assert (push.enabled, len(records)) == (False, 1)  # resolved, so it is outside the root


def test_the_key_may_come_from_the_environment_and_a_broken_one_or_a_missing_id_switches_off(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key may come from the environment and a broken one or a missing id switches off."""
    monkeypatch.setenv(KEY_ENV, _pem().decode().replace("\n", "\\n"))
    push, records = _boot(tmp_path, caplog, _config())
    assert (push.enabled, len(records)) == (True, 1)
    push, records = _boot(tmp_path, caplog, _config(key_id=""))
    assert (push.enabled, len(records)) == (False, 1)
    monkeypatch.setenv(KEY_ENV, "-----BEGIN PRIVATE KEY-----\nnot a key\n-----END PRIVATE KEY-----")
    push, records = _boot(tmp_path, caplog, _config())
    assert (push.enabled, len(records)) == (False, 1)
    assert "not a key" not in records[0].getMessage()
    other = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    monkeypatch.setenv(KEY_ENV, other.decode())
    push, records = _boot(tmp_path, caplog, _config())
    assert (push.enabled, len(records)) == (False, 1)


def test_no_log_line_holds_a_device_token_a_provider_token_or_the_text(
    world: World, caplog: pytest.LogCaptureFixture,
) -> None:
    """No log line holds a device token a provider token or the text."""
    world.register(live=LIVE_A)
    text = "the dentist at 4 pm, Dana's number"
    with caplog.at_level(logging.DEBUG):
        world.push.send("t", text)
        refusals = ((410, "Unregistered"), (400, "BadDeviceToken"), (403, "ExpiredProviderToken"))
        for status, reason in refusals:
            world.register(live=LIVE_A)
            world.sandbox.reply = _answering(status, reason)
            world.push.send("t", text)
            world.push.poll()
        world.sandbox.reply = lambda _r: (200, None)
        world.client._hosts = {"sandbox": "http://127.0.0.1:1", "production": "x"}  # noqa: SLF001
        world.register()
        world.push.send("t", text)
    seen = "\n".join(r.getMessage() + str(r.exc_info) for r in caplog.records)
    assert "push:" in seen
    jwts = {_bearer(r) for r in world.requests()}
    for leaked in (DEVICE_A, DEVICE_B, LIVE_A, text, *jwts):
        assert leaked not in seen
