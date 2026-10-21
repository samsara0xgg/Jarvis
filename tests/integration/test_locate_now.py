"""ADR 0281: ``where_am_i`` asks his phone for a fresh fix and falls back to its last report.

Acceptance checks against the real ``/phone/ws`` route and phone-events route on a real uvicorn
server, the real event log, the real :class:`~jarvis.runtime.locate.Locator` and the sender over
the in-process fake APNs of ``test_push``. The phone is the fake client; no key, no Apple:

- a phone with a live socket is sent ``locate``; its ``located`` in time is a fresh fix, stored
  as an ordinary location report carrying the request id; one that comes late, one that says
  ``denied`` or ``unavailable`` and one that is malformed leave the last report as the answer,
  with its age and why it is not fresh (and a refusal sends no push);
- a phone with no socket gets the background push with exactly its payload and headers, and a
  location report POSTed with the request id completes the wait; a silent socket escalates to
  the push; with neither, the last report answers at once;
- request ids: the shape on the wire, in the push and in a report, and a report carrying one that
  nobody waits for is only a report;
- the tool's words: fresh, fallback and snapshot.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import json
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest
import uvicorn

from jarvis.execution.location_tool import build_where_tool
from jarvis.runtime.daemon import output_watchers
from jarvis.runtime.locate import Locator
from jarvis.state.device_tokens import device_name_for_token, device_token_matches
from jarvis.state.event_log import emit_event
from jarvis.state.locate_wait import LocateWaits, valid_request_id
from jarvis.state.phone_location import NoPhoneReport
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface.apns import locate_payload
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_events import PHONE_EVENTS_PATH, payload_valid
from jarvis.surface.phone_link import PHONE_PATH, PhoneHub
from jarvis.surface.terminal_events import BrainEvents
from tests.support.net import free_port
from tests.support.push import BUNDLE, DEVICE_A, FakeApns, World
from tests.support.terminal import AsRemote, wait_for
from tools.phone_fake_client import FakePhone

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

LAT, LNG = 48.4634, -123.3117
SNAPSHOT = "ab" * 16


class Host:
    """The real phone socket and phone-events routes over the world's event log."""

    def __init__(self, world: World, waits: LocateWaits) -> None:
        """Serve for the world's paired phone; ``waits`` is where its answers are delivered."""
        self.world = world
        self.waits = waits
        self.port = free_port()
        self.hub: PhoneHub | None = None
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    async def _main(self) -> None:
        conn = sqlite3.connect(self.world.paths.event_log, check_same_thread=False)
        events = BrainEvents(conn, self.waits)
        self.hub = PhoneHub(events=events, rows=output_watchers.PhoneRows(conn))
        app = create_app(InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            phone=self.hub,
            phone_events=events,
            device_name=functools.partial(device_name_for_token, self.world.root),
        ))
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.world.root)),
            device_token_matches=functools.partial(device_token_matches, self.world.root),
        )
        self.server = uvicorn.Server(uvicorn.Config(
            AsRemote(app), host="127.0.0.1", port=self.port, log_level="warning", lifespan="off",
        ))
        await self.server.serve()

    def __enter__(self) -> Self:
        """Start serving and wait until it is up."""
        self.thread.start()
        wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        """Stop serving."""
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    def phone(self) -> FakePhone:
        """A text-only phone paired as ``phone``, already past its hello."""
        phone = FakePhone(
            f"ws://127.0.0.1:{self.port}{PHONE_PATH}", self.world.phone_token, voice=False,
        ).start()
        phone.hello()
        return phone

    def post_report(self, payload: dict[str, Any], ts_ms: int, uid: str) -> list[dict[str, Any]]:
        """What the woken phone does: POST one location report, as the iOS Report layer does."""
        frame = {
            "event_uid": uid, "event_type": "phone.location_observed", "ts_epoch_ms": ts_ms,
            "payload": payload,
        }
        reply = httpx.post(
            f"http://127.0.0.1:{self.port}{PHONE_EVENTS_PATH}", json={"events": [frame]},
            headers={"Authorization": f"Bearer {self.world.phone_token}"}, timeout=10,
        )
        assert reply.status_code == httpx.codes.OK, reply.text
        return list(reply.json()["acks"])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    """A runtime root with a paired phone that registered a sandbox device token."""
    with FakeApns() as sandbox, FakeApns() as production:
        one = World(tmp_path, sandbox, production)
        one.register()
        yield one
        one.close()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _locator(  # noqa: PLR0913 - the fixture's pieces and the two waits.
    world: World, waits: LocateWaits, hub: PhoneHub | None, *, socket_s: float = 3.0,
    push_s: float = 3.0, push: bool = True,
) -> Locator:
    locator = Locator(
        world.paths.event_log, waits, socket_wait_s=socket_s, push_wait_s=push_s,
    )
    if hub is not None:
        locator.ask_socket = hub.ask_location
    if push:
        locator.ask_push = world.push.locate
    return locator


def _last_report(world: World, minutes_ago: int = 18) -> None:
    emit_event(
        world.log, type="phone.location_observed", ingestion_node="phone",
        payload={"lat": 48.5, "lng": -123.4, "accuracy_m": 65.0, "place": "Old place"},
        ts_epoch_ms=_now_ms() - minutes_ago * 60_000,
    )


def _located(request_id: str, **more: Any) -> dict[str, Any]:  # noqa: ANN401
    return {
        "type": "located", "request_id": request_id, "lat": LAT, "lng": LNG, "accuracy_m": 9.5,
        "fix_ts_epoch_ms": _now_ms() - 1500, **more,
    }


def _locations(world: World) -> list[dict[str, Any]]:
    rows = world.log.execute(
        "SELECT payload_json FROM events WHERE type = 'phone.location_observed' ORDER BY id",
    ).fetchall()
    return [json.loads(raw) for (raw,) in rows]


# --- a phone with a live socket ---------------------------------------------------------------


def test_a_located_in_time_is_a_fresh_fix_stored_as_a_report(world: World) -> None:
    """The ``locate`` frame reaches the phone; its ``located`` is the answer and a report."""
    waits = LocateWaits()
    _last_report(world)
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        phone = host.phone()
        try:
            locator = _locator(world, waits, host.hub, push=False)
            running = pool.submit(locator)
            asked = phone.wait_text("locate")
            assert set(asked) == {"type", "request_id"}
            assert valid_request_id(asked["request_id"])
            phone.send(_located(asked["request_id"], snapshot=SNAPSHOT))
            got = running.result(timeout=10)
        finally:
            phone.close()
    assert got["fresh"] is True
    assert (got["lat"], got["lng"], got["accuracy_m"]) == (LAT, LNG, 9.5)
    assert 1 <= got["age_s"] <= 5
    assert (got["device"], got["source"], got["snapshot"]) == ("phone", "phone", SNAPSHOT)
    assert "not_fresh" not in got
    stored = _locations(world)[-1]
    assert (stored["lat"], stored["lng"], stored["request_id"]) == (
        LAT, LNG, asked["request_id"],
    )
    assert stored["snapshot"] == SNAPSHOT
    assert not world.requests()  # an answer over the socket needs no push


def test_a_late_located_leaves_the_last_report_with_its_age_and_is_still_stored(
    world: World,
) -> None:
    """After the wait the answer is the last report (18 min old), the late fix only a report."""
    waits = LocateWaits()
    _last_report(world, minutes_ago=18)
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        phone = host.phone()
        try:
            locator = _locator(world, waits, host.hub, socket_s=0.3, push=False)
            running = pool.submit(locator)
            asked = phone.wait_text("locate")
            got = running.result(timeout=10)
            phone.send(_located(asked["request_id"]))
            wait_for(lambda: len(_locations(world)) == 2, "the late fix was not stored")
        finally:
            phone.close()
    assert got["fresh"] is False
    assert got["place"] == "Old place"
    assert 18 * 60 <= got["age_s"] <= 18 * 60 + 30
    assert "did not answer within 0.3 s" in got["not_fresh"]
    assert _locations(world)[-1]["request_id"] == asked["request_id"]


@pytest.mark.parametrize("error", ["denied", "unavailable"])
def test_a_located_error_is_a_fallback_at_once_and_sends_no_push(
    world: World, error: str,
) -> None:
    """The phone said it cannot: the last report answers without waiting out the clock."""
    waits = LocateWaits()
    _last_report(world)
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        phone = host.phone()
        try:
            locator = _locator(world, waits, host.hub, socket_s=5.0, push_s=5.0)
            started = time.monotonic()
            running = pool.submit(locator)
            asked = phone.wait_text("locate")
            phone.send({"type": "located", "request_id": asked["request_id"], "error": error})
            got = running.result(timeout=10)
        finally:
            phone.close()
    assert time.monotonic() - started < 4
    assert got["fresh"] is False
    assert got["not_fresh"] == f"the phone said {error}"
    assert not world.requests()
    assert len(_locations(world)) == 1


def test_a_malformed_located_is_refused_and_does_not_hold_the_wait(world: World) -> None:
    """A fix with no number in it is ``bad_located``; the wait ends as if the phone could not."""
    waits = LocateWaits()
    _last_report(world)
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        phone = host.phone()
        try:
            locator = _locator(world, waits, host.hub, socket_s=5.0, push=False)
            running = pool.submit(locator)
            asked = phone.wait_text("locate")
            phone.send({**_located(asked["request_id"]), "lat": "north"})
            got = running.result(timeout=10)
            assert phone.wait_text("error")["code"] == "bad_located"
            seen = len(phone.texts)
            phone.send({"type": "located", "request_id": "not valid!", "error": "denied"})
            phone.send({"type": "located", "request_id": "ok_id", "error": "later"})
            wait_for(lambda: len(phone.texts) >= seen + 2, "no errors came back")
            assert [f["code"] for f in phone.texts[seen:]] == ["bad_located"] * 2
        finally:
            phone.close()
    assert got["fresh"] is False
    assert len(_locations(world)) == 1


def test_a_located_nobody_waits_for_is_only_a_report(world: World) -> None:
    """Unsolicited, or after the wait ended, it is stored and answers nothing."""
    waits = LocateWaits()
    with Host(world, waits) as host:
        phone = host.phone()
        try:
            phone.send(_located("nobody-asked"))
            wait_for(lambda: len(_locations(world)) == 1, "the fix was not stored")
        finally:
            phone.close()
    assert _locations(world)[0]["request_id"] == "nobody-asked"


# --- a phone with no socket --------------------------------------------------------------------


def test_with_no_socket_the_background_push_goes_out_and_a_posted_report_completes_the_wait(
    world: World,
) -> None:
    """Exact payload and headers; the POSTed report with the request id is the answer."""
    waits = LocateWaits()
    _last_report(world)
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        locator = _locator(world, waits, host.hub, push_s=5.0)
        running = pool.submit(locator)
        wait_for(lambda: bool(world.sandbox.requests), "no push went out")
        [request] = world.sandbox.requests
        request_id = request["body"]["locate"]
        assert request["body"] == {"aps": {"content-available": 1}, "locate": request_id}
        assert valid_request_id(request_id)
        headers = request["headers"]
        assert headers["apns-push-type"] == "background"
        assert headers["apns-priority"] == "5"
        assert headers["apns-topic"] == BUNDLE
        assert headers["apns-expiration"] == "0"
        assert request["device"] == DEVICE_A
        assert request["path"] == f"/3/device/{DEVICE_A}"
        assert not running.done()
        acks = host.post_report(
            {"lat": LAT, "lng": LNG, "accuracy_m": 12.0, "request_id": request_id},
            _now_ms() - 3000, "cd" * 16,
        )
        assert acks[0]["ok"] is True
        got = running.result(timeout=10)
    assert got["fresh"] is True
    assert (got["lat"], got["lng"], got["accuracy_m"]) == (LAT, LNG, 12.0)
    assert 2 <= got["age_s"] <= 6
    assert _locations(world)[-1]["request_id"] == request_id


def test_a_push_nobody_answers_falls_back_to_the_last_report_with_its_age(
    world: World,
) -> None:
    """The phone never wakes: after the push wait the answer is the 20 minute old report."""
    waits = LocateWaits()
    _last_report(world, minutes_ago=20)
    with Host(world, waits) as host:
        got = _locator(world, waits, host.hub, push_s=0.3)()
    assert got["fresh"] is False
    assert 20 * 60 <= got["age_s"] <= 20 * 60 + 30
    assert "did not answer the push within 0.3 s" in got["not_fresh"]


def test_a_silent_socket_escalates_to_the_push(world: World) -> None:
    """An app iOS suspended keeps its socket open and answers nothing: the push wakes it."""
    waits = LocateWaits()
    with Host(world, waits) as host, concurrent.futures.ThreadPoolExecutor(1) as pool:
        phone = host.phone()
        try:
            running = pool.submit(_locator(world, waits, host.hub, socket_s=0.3, push_s=5.0))
            phone.wait_text("locate")
            wait_for(lambda: bool(world.sandbox.requests), "the push never went out")
            request_id = world.sandbox.requests[0]["body"]["locate"]
            host.post_report(
                {"lat": LAT, "lng": LNG, "accuracy_m": 30.0, "request_id": request_id},
                _now_ms() - 1000, "ef" * 16,
            )
            got = running.result(timeout=10)
        finally:
            phone.close()
    assert got["fresh"] is True


def test_with_no_way_to_ask_the_last_report_answers_at_once(world: World) -> None:
    """No socket and no push key: no wait at all, and with no report at all the usual error."""
    waits = LocateWaits()
    locator = _locator(world, waits, None, socket_s=5.0, push_s=5.0, push=False)
    with pytest.raises(NoPhoneReport):
        locator()
    _last_report(world)
    started = time.monotonic()
    got = locator()
    assert time.monotonic() - started < 1
    assert got["fresh"] is False
    assert got["not_fresh"] == "no phone could be asked"


def test_a_dead_push_token_is_dropped_and_the_report_answers(world: World) -> None:
    """APNs 410: the token is forgotten (as for any push), the wait is not spent on it."""
    waits = LocateWaits()
    _last_report(world)
    world.sandbox.reply = lambda _r: (410, "Unregistered")
    started = time.monotonic()
    got = _locator(world, waits, None, push_s=5.0)()
    assert time.monotonic() - started < 1
    assert got["not_fresh"] == "no phone could be asked"
    assert len(world.sandbox.requests) == 1


# --- request ids and the report's fields ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("a", True), ("A_b-9", True), ("x" * 64, True), ("x" * 65, False), ("", False),
     ("a b", False), ("é", False), ("a/b", False), (7, False), (None, False)],
)
def test_a_request_id_is_one_to_sixty_four_of_letters_digits_underscore_and_hyphen(
    value: object, expected: bool,  # noqa: FBT001 - a table column.
) -> None:
    """The shape holds on the wire, in the push and in a stored report."""
    assert valid_request_id(value) is expected
    report = {"lat": 1.0, "lng": 2.0, "accuracy_m": 3.0, "request_id": value}
    assert payload_valid("phone.location_observed", report, _now_ms()) is expected
    if isinstance(value, str):
        if expected:
            assert json.loads(locate_payload(value)) == {
                "aps": {"content-available": 1}, "locate": value,
            }
        else:
            with pytest.raises(ValueError, match="request_id"):
                locate_payload(value)


def test_a_report_may_carry_a_snapshot_id_and_nothing_else_new() -> None:
    """``snapshot`` is an attachment id; any other extra key is still refused."""
    base = {"lat": 1.0, "lng": 2.0, "accuracy_m": 3.0}
    assert payload_valid("phone.location_observed", {**base, "snapshot": SNAPSHOT}, _now_ms())
    assert not payload_valid("phone.location_observed", {**base, "snapshot": "../x"}, _now_ms())
    assert not payload_valid("phone.location_observed", {**base, "request": "a"}, _now_ms())


# --- what the model reads -----------------------------------------------------------------------


def _where(reading: dict[str, Any]) -> dict[str, Any]:
    (tool,) = build_where_tool(None, lambda: reading)
    return dict(tool.handler({}, type("Ctx", (), {"conn": None, "action_id": "A1"})()))


def test_the_tool_says_a_fix_is_fresh_and_where_its_picture_is() -> None:
    """A fresh fix: seconds old, accuracy, and the snapshot to look at."""
    result = _where({
        "lat": LAT, "lng": LNG, "accuracy_m": 9.5, "age_s": 2, "source": "phone",
        "device": "phone", "fresh": True, "snapshot": SNAPSHOT, "place": "UVic",
    })
    assert result["fresh"] is True
    assert result["snapshot"] == SNAPSHOT
    assert result["place"] == "UVic"
    assert "just took, 2 s old" in result["note"]
    assert f"look_at_picture, picture {SNAPSHOT[:8]}" in result["note"]


def test_the_tool_says_a_fallback_is_the_last_report_and_why() -> None:
    """The last report: its age in minutes, the 500 m rule, and that a fresh fix did not come."""
    result = _where({
        "lat": LAT, "lng": LNG, "accuracy_m": 65.0, "age_s": 18 * 60, "source": "phone",
        "device": "phone", "fresh": False, "not_fresh": "the phone said denied",
    })
    assert result["fresh"] is False
    assert "last report 18 min ago" in result["note"]
    assert "500 m" in result["note"]
    assert "did not come (the phone said denied)" in result["note"]
    assert "snapshot" not in result


def test_a_reader_without_the_new_keys_reads_as_before() -> None:
    """The phone's plain last report (a Mac running alone, the bus tool) is unchanged."""
    result = _where({
        "lat": LAT, "lng": LNG, "accuracy_m": 65.0, "age_s": 300, "source": "phone",
        "device": "phone",
    })
    assert "fresh" not in result
    assert "did not come" not in result["note"]
    assert result["note"].startswith("the phone's last report 5 min ago")
