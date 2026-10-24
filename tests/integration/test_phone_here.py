"""ADR 0198, 0212, 0322: where he is for a tool call, and which device and report say so.

Acceptance checks, each against a real event log:

- which report is the latest, as a table of log rows to the report: a location at its event time,
  a visit at its departure (else its arrival), the latest time wins, the later row on a tie, any
  device, no report at all;
- the whole path: a batch posted to ``/inherent/device/events`` is what Here reads;
- the policy, through the dispatcher and the real tools on a fake clock (nothing sleeps): the
  carried phone's recent report is used without asking and the receipt carries it; an old one asks
  that phone only; no answer gives the last place with its age and says he may have moved; an iPad
  turn reads the iPad; a Mac terminal on a brain reads ``locate.phone`` even when the iPad's
  report is newer; ``where_am_i`` asks however fresh the report; ``save_place`` goes through Here;
- the turn's opening row to the reading it gets on a host that has a Mac reader too;
- the place line of the state block, inside and past the age limit;
- a brain booted for real wires ``locate.phone`` and ``locate.max_age_s``;
- the age in words, and the HTTP routes a phone uses writing the turn's opening row under the
  device's name.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.deployment import bootstrap_runtime
from jarvis.execution.location_tool import describe_age
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.locate import NO_PHONE_REPORT, NOT_ASKED, HerePolicy, Locator
from jarvis.runtime.whereabouts import Whereabouts
from jarvis.shared import ActionRequest, CallerPrincipal
from jarvis.state import places as place_state
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import (
    MAC_NODE,
    emit_event,
    open_event_log,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.state.locate_wait import Located, LocateWaits
from jarvis.state.memory_db import open_memory_db
from jarvis.state.phone_location import NoPhoneReport, latest_phone_fix
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from tests.support.bus_world import google_place, serve_text_search
from tests.support.clock import FakeClock
from tests.support.phone import GOOD, LOCATION, PNG, VISIT, event_frame, here_daemon
from tests.support.runtime_config import boot_runtime
from tests.support.terminal import bearer, brain_client
from tests.support.tool_world import ToolWorld, tool_chain, tool_request
from tests.support.transit import FIX as MAC_FIX
from tests.support.transit import TRANSIT_NOW, serve_routes

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

T = 1_760_000_000_000
"""The clock of the table: a report is aged against it."""
MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR

Row = tuple[str, int, dict[str, Any], str]
"""A row of the log: event type, event time, payload, ingestion node."""


def loc(
    ts: int, lat: float, lng: float, *, acc: float = 50.0, device: str = "phone",
    **more: Any,  # noqa: ANN401
) -> Row:
    """A location fix reported at ``ts``."""
    payload = {"lat": lat, "lng": lng, "accuracy_m": acc, **more}
    return ("phone.location_observed", ts, payload, device)


def visit(  # noqa: PLR0913 - a visit is a row, and each argument is one column of it
    ts: int, arrived: int, departed: int | None, lat: float, lng: float, *,
    acc: float = 65.0, device: str = "phone", **more: Any,  # noqa: ANN401
) -> Row:
    """A visit reported at ``ts``, with a departure when it has one."""
    times = {"arrived_at_ms": arrived} | ({} if departed is None else {"departed_at_ms": departed})
    payload = {"lat": lat, "lng": lng, "accuracy_m": acc, **times, **more}
    return ("phone.visit_observed", ts, payload, device)


def write(path: Path, rows: list[Row]) -> sqlite3.Connection:
    """A log holding ``rows`` in this order."""
    conn = open_event_log(path)
    for kind, ts, payload, device in rows:
        emit_event(conn, type=kind, payload=payload, ts_epoch_ms=ts, ingestion_node=device)
    return conn


def expect(
    lat: float, lng: float, acc: float, age_s: int, device: str = "phone", **place: str,
) -> dict[str, Any]:
    """The report a row means, with its age against :data:`T`."""
    return {"lat": lat, "lng": lng, "accuracy_m": acc, "age_s": age_s, "device": device, **place}


UVIC: dict[str, Any] = {"place": "University of Victoria"}
NOISE: list[Row] = [
    ("phone.state_observed", T - MIN, {"signal": "power", "value": "connected"}, "phone"),
    ("phone.motion_observed", T - MIN,
     {"activity": "walking", "confidence": "high", "started_at_ms": T - 2 * MIN}, "phone"),
    ("phone.health_observed", T - MIN,
     {"metric": "steps", "start_ms": T - HOUR, "end_ms": T - MIN, "value": 900}, "phone"),
    ("repo.state_observed", T - MIN,
     {"repo_path": "/r", "branch": "main", "head_sha": "a", "dirty_file_count": 0,
      "last_commit_subject": "s", "observed_at_ms": 1, "actor": "observer"}, "macbook"),
]

CASES: dict[str, tuple[list[Row], dict[str, Any] | None]] = {
    "an empty log has no report": ([], None),
    "other phone signals and other devices' events are not a place": (NOISE, None),
    "a location is where he is at its event time": (
        [loc(T - 90_000, 48.46, -123.31)], expect(48.46, -123.31, 50.0, 90),
    ),
    "a location keeps its place name": (
        [loc(T - 5 * MIN, 48.46, -123.31, acc=120, **UVIC)],
        expect(48.46, -123.31, 120.0, 300, **UVIC),
    ),
    "a visit without a departure is at its arrival": (
        [visit(T - 2 * MIN, T - 3 * HOUR, None, 48.4, -123.4, **UVIC)],
        expect(48.4, -123.4, 65.0, 3 * 3600, **UVIC),
    ),
    "a visit with a departure is at its departure": (
        [visit(T - 2 * MIN, T - 3 * HOUR, T - HOUR, 48.4, -123.4)],
        expect(48.4, -123.4, 65.0, 3600),
    ),
    "a location after a visit's arrival is later": (
        [visit(T - HOUR, T - 3 * HOUR, None, 48.4, -123.4), loc(T - 10 * MIN, 48.5, -123.5)],
        expect(48.5, -123.5, 50.0, 600),
    ),
    "a visit's arrival after the last location is later": (
        [loc(T - HOUR, 48.5, -123.5), visit(T - 20 * MIN, T - 30 * MIN, None, 48.4, -123.4)],
        expect(48.4, -123.4, 65.0, 30 * 60),
    ),
    "a departure after the last location is later, though its row is older": (
        [visit(T - 40 * MIN, T - 3 * HOUR, T - 45 * MIN, 48.4, -123.4),
         loc(T - 50 * MIN, 48.5, -123.5)],
        expect(48.4, -123.4, 65.0, 45 * 60),
    ),
    "a location after a visit's departure is later, though its row is older": (
        [loc(T - 20 * MIN, 48.5, -123.5),
         visit(T - 10 * MIN, T - 3 * HOUR, T - 25 * MIN, 48.4, -123.4)],
        expect(48.5, -123.5, 50.0, 20 * 60),
    ),
    "a tie goes to the later row, a location after a visit": (
        [visit(T - 8 * MIN, T - 10 * MIN, None, 48.4, -123.4), loc(T - 10 * MIN, 48.5, -123.5)],
        expect(48.5, -123.5, 50.0, 600),
    ),
    "a tie goes to the later row, a visit after a location": (
        [loc(T - 10 * MIN, 48.5, -123.5), visit(T - 8 * MIN, T - 10 * MIN, None, 48.4, -123.4)],
        expect(48.4, -123.4, 65.0, 600),
    ),
    "a tie goes to the later row, a departure and a location": (
        [loc(T - 10 * MIN, 48.5, -123.5),
         visit(T - 8 * MIN, T - HOUR, T - 10 * MIN, 48.4, -123.4)],
        expect(48.4, -123.4, 65.0, 600),
    ),
    "a tie between two locations goes to the later row": (
        [loc(T - MIN, 48.5, -123.5), loc(T - MIN, 48.6, -123.6)],
        expect(48.6, -123.6, 50.0, 60),
    ),
    "the latest report of any device wins and names its device": (
        [loc(T - 30 * MIN, 48.5, -123.5, device="iphone"),
         loc(T - 10 * MIN, 48.6, -123.6, device="ipad"),
         loc(T - 20 * MIN, 48.7, -123.7, device="iphone")],
        expect(48.6, -123.6, 50.0, 600, device="ipad"),
    ),
    "a report ten days old is still where he is": (
        [loc(T - 10 * DAY, 48.5, -123.5)], expect(48.5, -123.5, 50.0, 10 * 24 * 3600),
    ),
    "a time ahead of the clock is no age": (
        [loc(T + 2 * MIN, 48.5, -123.5)], expect(48.5, -123.5, 50.0, 0),
    ),
    "the age is whole seconds, rounded down": (
        [loc(T - 1_999, 48.5, -123.5)], expect(48.5, -123.5, 50.0, 1),
    ),
    "the noise around a report changes nothing": (
        [*NOISE, loc(T - 7 * MIN, 48.5, -123.5), *NOISE], expect(48.5, -123.5, 50.0, 420),
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_latest_report_is_the_one_a_row_table_says(name: str, tmp_path: Path) -> None:
    """Log rows in, the latest report out: position, accuracy, place if any, age and device."""
    rows, expected = CASES[name]
    conn = write(tmp_path / "events.db", rows)
    try:
        fix = latest_phone_fix(conn)
        if expected is None:
            assert fix is None
        else:
            assert fix is not None
            got = expect(
                fix.lat, fix.lng, fix.accuracy_m, max(0, T - fix.at_ms) // 1000, fix.device,
                **({"place": fix.place} if fix.place else {}),
            )
            assert got == expected
    finally:
        conn.close()


def test_a_report_of_another_device_is_never_the_named_ones(tmp_path: Path) -> None:
    """Asked for a device, the latest report is that device's, newer ones of others aside."""
    conn = write(tmp_path / "events.db", [
        loc(T - 30 * MIN, 48.5, -123.5, device="iphone"),
        loc(T - 10 * MIN, 48.6, -123.6, device="ipad"),
    ])
    try:
        iphone = latest_phone_fix(conn, "iphone")
        assert iphone is not None
        assert (iphone.lat, iphone.device) == (48.5, "iphone")
        assert latest_phone_fix(conn, "watch") is None
    finally:
        conn.close()


def test_a_batch_posted_to_the_route_is_what_here_reads(tmp_path: Path) -> None:
    """Phone to brain to Here: a visit, then a newer location, through the real route and log."""
    token = pair_device(tmp_path, "iphone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    now = int(time.time() * 1000)
    visit_payload = {**GOOD[VISIT], "arrived_at_ms": now - 3 * HOUR, "departed_at_ms": now - HOUR}
    fix_payload = {**GOOD[LOCATION], "lat": 48.43, "lng": -123.37, "accuracy_m": 80}
    batch = [
        {**event_frame(1, VISIT, visit_payload), "ts_epoch_ms": now - HOUR},
        {**event_frame(2, LOCATION, fix_payload), "ts_epoch_ms": now - 20 * MIN},
    ]
    posted = client.post(PHONE_EVENTS_PATH, json={"events": batch}, headers=bearer(token))
    assert [ack["ok"] for ack in posted.json()["acks"]] == [True, True]
    clock = FakeClock(datetime.fromtimestamp(now / 1000, UTC))
    here = HerePolicy(log, clock, max_age_s=3600).bind(None).read()
    assert here.device == "iphone"
    assert (here.lat, here.lng, here.accuracy_m) == (48.43, -123.37, 80.0)
    assert here.place == fix_payload["place"]
    assert here.age_s == 20 * 60
    assert (here.fresh, here.why) == (False, "")


# --- the policy, through the dispatcher ------------------------------------------------------

IPHONE, IPAD = (48.50, -123.50), (48.60, -123.60)
ANSWER = (48.70, -123.70)
"""Where a phone says it is when it is asked."""


MALL = {
    "Mayfair Mall": google_place(
        "Mayfair Mall", "3147 Douglas St, Victoria, BC", 48.4400, -123.3600,
        "ChIJmayfair0123456789ab",
    ),
}


class Phones:
    """The phones a Locator can ask: records who was asked and answers for those that will.

    A phone not in ``answering`` has no socket and no push token, so nothing is sent to it.
    """

    def __init__(self, waits: LocateWaits, clock: FakeClock, answering: dict[str, bool]) -> None:
        """``answering[name]`` is whether that phone answers a request for a fix."""
        self.waits = waits
        self.clock = clock
        self.answering = answering
        self.asked: list[str | None] = []

    def socket(self, request_id: str, device: str | None) -> int:
        """The ``locate`` frame sent to ``device`` (every phone when ``None``)."""
        self.asked.append(device)
        names = [n for n in ([device] if device else self.answering) if n in self.answering]
        for name in names:
            if self.answering[name]:
                self.waits.deliver(request_id, Located(
                    *ANSWER, 12.0, self.clock.time_ms() - 1000, name, place="Asked Rd",
                ))
        return len(names)

    def push(self, _request_id: str, device: str | None) -> int:
        """A background push that wakes nothing, sent to each phone there is."""
        return len([n for n in ([device] if device else self.answering) if n in self.answering])


class Rig:
    """A runtime root, a fake clock, Here over the log and the real tools over both."""

    def __init__(
        self, root: Path, monkeypatch: pytest.MonkeyPatch, *, phone: str = "iphone",
        answering: dict[str, bool] | None = None, mac: bool = False,
    ) -> None:
        """Wire a registry whose tools read Here; ``mac`` gives the host a reader of its own."""
        self.clock = FakeClock(TRANSIT_NOW)
        self.seen = serve_routes(monkeypatch)
        serve_text_search(monkeypatch, MALL)
        self.memory = root / "memory.db"
        open_memory_db(self.memory).close()
        waits = LocateWaits()
        self.phones = Phones(waits, self.clock, answering or {})
        locator = Locator(waits, self.clock)
        locator.ask_socket = self.phones.socket
        locator.ask_push = self.phones.push
        self.here = HerePolicy(
            bootstrap_runtime(root / "run").event_log, self.clock, locator=locator, phone=phone,
            mac=(lambda: dict(MAC_FIX)) if mac else None, memory_db=self.memory,
        )
        registry = build_default_registry(
            transit_api_key="test-key-not-real", transit_places=dict, memory_db_path=self.memory,
            clock=self.clock, here=self.here,
        )
        self.world = ToolWorld(
            root / "run", tools=tuple(registry.get_definitions()), clock=self.clock, here=self.here,
        )
        self.conn = self.world.conn
        self.n = 0

    def report(self, device: str, ago_s: int, at: tuple[float, float], **more: Any) -> None:  # noqa: ANN401
        """``device`` reported ``at`` ``ago_s`` seconds ago."""
        emit_event(
            self.conn, type="phone.location_observed", ingestion_node=device,
            payload={"lat": at[0], "lng": at[1], "accuracy_m": 30.0, **more},
            ts_epoch_ms=self.clock.time_ms() - ago_s * 1000,
        )

    def open_turn(self, turn_id: str, node: str) -> None:
        """The words that open ``turn_id`` came from ``node``."""
        emit_event(
            self.conn, type="surface.user_intent", ingestion_node=node,
            payload={"transcript": "where", "turn_id": turn_id, "channel": "cli_stdin"},
            correlation={"turn_id": turn_id},
        )

    def call(
        self, name: str, args: dict[str, Any], turn_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Dispatch ``name`` as the model would; the output and the receipt."""
        self.n += 1
        action_id = f"A{self.n}"
        request = dataclasses.replace(
            tool_request(name, action_id, arguments=args), turn_id=turn_id,
        )
        self.world.dispatch(request)
        observed = tool_chain(self.world, action_id)
        return json.loads(observed["tool_output"]), observed["receipt"]

    def origin(self) -> tuple[float, float]:
        """Where the last Routes request started from."""
        latlng = json.loads(self.seen[-1][0].data)["origin"]["location"]["latLng"]
        return latlng["latitude"], latlng["longitude"]

    def close(self) -> None:
        """Close the log."""
        self.world.close()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    """Builds a rig; every one made is closed."""
    made: list[Rig] = []

    def build(**more: Any) -> Rig:  # noqa: ANN401
        one = Rig(tmp_path, monkeypatch, **more)
        made.append(one)
        return one

    yield build
    for one in made:
        one.close()


def test_a_two_minute_old_report_of_the_carried_phone_is_used_without_asking(rig: Any) -> None:  # noqa: ANN401
    """The bus starts from it, says how old it is, and the receipt carries the reading."""
    r: Rig = rig(answering={"iphone": True, "ipad": True})
    r.report("iphone", 120, IPHONE, place="Home Rd")
    r.report("ipad", 30, IPAD)  # newer, but not the phone he carries
    out, receipt = r.call("bus", {"to": "Mayfair Mall"})
    assert r.phones.asked == []
    assert r.origin() == IPHONE
    assert out["from"] == "here (the phone's last report 2 min ago, Home Rd, ±30 m)"
    assert "warning" not in out
    assert receipt["here"] == {
        "lat": IPHONE[0], "lng": IPHONE[1], "accuracy_m": 30.0, "age_s": 120,
        "device": "iphone", "fresh": False, "place": "Home Rd",
    }


def test_an_old_report_asks_only_the_carried_phone_and_the_receipt_says_fresh(rig: Any) -> None:  # noqa: ANN401
    """Past the limit the carried phone is asked, no other, and its answer is the reading."""
    r: Rig = rig(answering={"iphone": True, "ipad": True})
    r.report("iphone", 20 * 60, IPHONE)
    r.report("ipad", 5 * 60, IPAD)
    out, receipt = r.call("bus", {"to": "Mayfair Mall"})
    assert r.phones.asked == ["iphone"]
    assert r.origin() == ANSWER
    assert receipt["here"]["fresh"] is True
    assert receipt["here"]["device"] == "iphone"
    assert (receipt["here"]["lat"], receipt["here"]["lng"]) == ANSWER
    assert "warning" not in out


def test_no_answer_gives_the_last_place_with_its_age_and_says_he_may_have_moved(
    rig: Any,  # noqa: ANN401
) -> None:
    """Both waits pass on the clock: the last report, how old, whose, and that it may be stale."""
    r: Rig = rig(answering={"iphone": False})  # connected, and silent
    r.report("iphone", 20 * 60, IPHONE, place="Old Rd")
    def pass_both_waits() -> None:
        for seconds in (4.0, 8.0):  # the socket wait, then the push wait
            r.clock.wait_until(lambda: r.clock.blocked >= 1, "the locator to wait")
            r.clock.advance(seconds)

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        moving = pool.submit(pass_both_waits)
        out, receipt = r.call("where_am_i", {})  # on this thread: the log is this thread's
        moving.result(timeout=10)
    assert (out["place"], out["device"], out["fresh"]) == ("Old Rd", "iphone", False)
    assert out["age_s"] == 20 * 60 + 12
    assert out["not_fresh"] == "the phone did not answer the push within 8 s"
    assert "he may not be where this says now" in out["note"]
    assert receipt["here"]["why"] == out["not_fresh"]
    assert receipt["here"]["age_s"] == out["age_s"]


def test_an_ipad_turn_reads_the_ipad(rig: Any) -> None:  # noqa: ANN401
    """A turn a device that reports locations opened reads that device, not the carried phone."""
    r: Rig = rig(answering={"iphone": True, "ipad": True})
    r.report("iphone", 60, IPHONE)
    r.report("ipad", 90, IPAD)
    r.open_turn("T1", "ipad")
    _, receipt = r.call("bus", {"to": "Mayfair Mall"}, turn_id="T1")
    assert r.origin() == IPAD
    assert r.phones.asked == []
    assert receipt["here"]["device"] == "ipad"


@pytest.mark.parametrize(("carried", "device"), [("iphone", "iphone"), ("", "ipad")])
def test_a_mac_terminal_turn_on_a_brain_reads_the_carried_phone(
    rig: Any, carried: str, device: str,  # noqa: ANN401
) -> None:
    """The Mac's words on a host with no location of its own: ``locate.phone``, else the newest."""
    r: Rig = rig(phone=carried, answering={})
    r.report("iphone", 5 * 60, IPHONE)
    r.report("ipad", 60, IPAD)  # newer than the iPhone's
    r.open_turn("T1", MAC_NODE)
    _, receipt = r.call("bus", {"to": "Mayfair Mall"}, turn_id="T1")
    assert receipt["here"]["device"] == device
    assert r.origin() == (IPHONE if device == "iphone" else IPAD)
    assert r.phones.asked == []


def test_where_am_i_asks_even_when_the_report_is_ten_seconds_old(rig: Any) -> None:  # noqa: ANN401
    """It is the tool for where he is now: it always asks, and the receipt says the fix is fresh."""
    r: Rig = rig(answering={"iphone": True})
    r.report("iphone", 10, IPHONE)
    out, receipt = r.call("where_am_i", {})
    assert r.phones.asked == ["iphone"]
    assert out["fresh"] is True
    assert (out["lat"], out["lng"]) == ANSWER
    assert receipt["here"]["fresh"] is True


def test_save_place_here_goes_through_here(rig: Any) -> None:  # noqa: ANN401
    """The place is kept where the turn's device puts him, and refused when no fix can be had."""
    r: Rig = rig(answering={})
    r.report("iphone", 60, IPHONE)
    r.report("ipad", 90, IPAD)
    r.open_turn("T1", "ipad")
    out, receipt = r.call("save_place", {"name": "gym", "where": "here"}, turn_id="T1")
    assert out["kept"]["name"] == "gym"
    assert receipt["here"]["device"] == "ipad"
    (kept,) = place_state.read_kept(r.memory)
    assert (kept.lat, kept.lng) == IPAD


# --- a host that has a Mac reader too (ADR 0212) ------------------------------------------------

# (node that opened the turn or None, whether a phone has reported) -> (device read, why)
ORIGINS: dict[str, tuple[str | None, bool, tuple[str, str]]] = {
    "a phone's turn reads the phone": ("iphone", True, ("iphone", "")),
    "any paired device's name is a phone": ("ipad", True, ("ipad", "")),
    "the Mac's own turn reads the Mac, though a phone has reported": (
        MAC_NODE, True, (MAC_NODE, ""),
    ),
    "a turn with no opening row reads the carried phone": (None, True, ("iphone", "")),
    "a phone's turn before any report falls back to the Mac and says so": (
        "iphone", False, (MAC_NODE, NO_PHONE_REPORT),
    ),
    "the Mac's turn before any report is just the Mac": (MAC_NODE, False, (MAC_NODE, "")),
}


@pytest.mark.parametrize("name", list(ORIGINS))
def test_the_turns_opening_row_picks_the_device_it_reads(rig: Any, name: str) -> None:  # noqa: ANN401
    """On a Mac that hears phones: opening row and reports in, the reading's device and why out."""
    node, reported, (device, why) = ORIGINS[name]
    r: Rig = rig(mac=True, answering={})
    if reported:
        r.report("iphone", 5 * 60, IPHONE)
        r.report("ipad", 2 * 60, IPAD)
    if node is not None:
        r.open_turn("T1", node)
    got = r.here.bind("T1" if node else None).read()
    assert (got.device, got.why) == (device, why)
    if reported and device == "iphone":
        assert (got.lat, got.lng) == IPHONE
    if device == MAC_NODE:
        assert (got.lat, got.lng, got.place) == (MAC_FIX["lat"], MAC_FIX["lng"], MAC_FIX["place"])
        assert (got.fresh, got.age_s) == (True, 0)


def test_without_a_mac_reader_and_no_report_here_is_unavailable(rig: Any) -> None:  # noqa: ANN401
    """A brain: no phone has reported and none answers, so there is nothing to read."""
    r: Rig = rig(answering={})
    with pytest.raises(NoPhoneReport, match="no phone has reported a location"):
        r.here.bind(None).read()
    out, receipt = r.call("where_am_i", {})
    assert out["code"] == "location_unavailable"
    assert "here" not in receipt


# --- the place line of the state block ----------------------------------------------------------


def test_the_place_line_names_the_place_inside_the_limit_and_says_unknown_past_it(
    rig: Any,  # noqa: ANN401
) -> None:
    """Within ``locate.max_age_s`` the phone's place; past it, that it is unknown and why."""
    r: Rig = rig(answering={})
    r.report("iphone", 2 * 60, IPHONE, place="Lotus Rd")
    where = Whereabouts(r.world.paths.root, None, None, here=r.here)

    def place_line() -> str:
        return next(
            line for line in where.lines(r.conn) if line.startswith(("His phone", "His place"))
        )

    assert place_line() == "His phone last put him at Lotus Rd, 2 min ago."
    r.clock.advance(13 * 60)  # exactly the limit
    assert place_line() == "His phone last put him at Lotus Rd, 15 min ago."
    r.clock.advance(1)
    assert place_line() == (
        "His place is unknown now: his last position report, from iphone, is 15 min old."
        " where_am_i asks his phone."
    )
    assert not [line for line in where.lines(r.conn) if line.startswith("Around him")]


# --- a brain booted for real wires the two settings -------------------------------------------


def _brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: str) -> JarvisRuntime:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key-not-real")
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text(f"runtime:\n  role: brain\n{settings}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    return boot_runtime(root)


def _report(runtime: JarvisRuntime, device: str, lat: float, lng: float, ts: int) -> None:
    """A phone's report through the brain's own hub, as the route hands it over."""
    assert runtime.terminal_hub is not None
    assert runtime.terminal_hub.events is not None
    frame = {
        "event_uid": f"{time.time_ns():032x}", "event_type": "phone.location_observed",
        "ts_epoch_ms": ts, "payload": {"lat": lat, "lng": lng, "accuracy_m": 40.0},
    }
    [ack] = runtime.terminal_hub.events.record_phone_batch(device, [frame])
    assert ack["ok"] is True, ack


def _bus_on_a_tool_thread(runtime: JarvisRuntime, turn_id: str | None = None) -> dict[str, Any]:
    """Dispatch ``bus`` from here through the runtime's own registry, on a worker thread.

    The routes the test serves are for another day, so the trip is not found; what the call read
    is on its receipt.
    """
    action_id = f"A-{turn_id}"
    request = ActionRequest(
        action_id=action_id, tool_name="bus", target_entity_ref=None,
        caller_principal=CallerPrincipal.JARVIS_LLM, risk_level="L1",
        arguments={"to": "Mayfair Mall"}, authorization_lease=None, run_id=None,
        turn_id=turn_id,
    )

    def run() -> dict[str, Any]:
        with closing(sqlite3.connect(runtime.runtime_paths.event_log)) as conn:
            lifecycle = ActionLifecycle()
            lifecycle.register(action_id)
            lifecycle.transition(action_id, "authorized")
            runtime.tool_registry.dispatch(request, conn, runtime.runtime_paths, lifecycle)
            row = conn.execute(
                "SELECT payload_json FROM events WHERE type = 'action.result_observed'"
                " ORDER BY id DESC LIMIT 1",
            ).fetchone()
        return dict(json.loads(row[0])["receipt"])

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(run).result()


def test_a_booted_brain_reads_locate_phone_and_asks_past_locate_max_age_s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The carried phone is the named one even when another is newer; past the limit it asks."""
    seen = serve_routes(monkeypatch)
    serve_text_search(monkeypatch, MALL)
    brain = _brain(tmp_path, monkeypatch, "locate:\n  phone: iphone\n  max_age_s: 600\n")
    try:
        now = int(time.time() * 1000)
        _report(brain, "iphone", 48.50, -123.50, now - 5 * MIN)
        _report(brain, "ipad", 48.60, -123.60, now - MIN)
        first = _bus_on_a_tool_thread(brain)["here"]
        origin = json.loads(seen[-1][0].data)["origin"]["location"]["latLng"]
        assert (origin["latitude"], origin["longitude"]) == (48.50, -123.50)
        assert (first["device"], first["fresh"], "why" in first) == ("iphone", False, False)
        assert 5 * 60 <= first["age_s"] < 5 * 60 + 30
        # A watch's report 12 minutes old is past the 600 s limit; no phone is connected to ask,
        # and the receipt of a turn the watch opened says so.
        _report(brain, "watch", 48.51, -123.51, now - 12 * MIN)
        emit_event(
            brain.conn, type="surface.user_intent", ingestion_node="watch",
            payload={"transcript": "bus", "turn_id": "T2", "channel": "cli_stdin"},
            correlation={"turn_id": "T2"},
        )
        old = _bus_on_a_tool_thread(brain, "T2")["here"]
        assert (old["device"], old["why"]) == ("watch", NOT_ASKED)
    finally:
        brain.conn.close()


@pytest.mark.parametrize(("seconds", "words"), [
    (0, "under a minute"), (59, "under a minute"), (60, "1 min"), (719, "11 min"),
    (3599, "59 min"), (3600, "1 h"), (3660, "1 h 1 min"), (86_399, "23 h 59 min"),
    (86_400, "1 d"), (90_000, "1 d 1 h"), (864_000, "10 d"),
])
def test_the_age_is_told_in_words(seconds: int, words: str) -> None:
    """What the model reads for how old a report is."""
    assert describe_age(seconds) == words


# --- the routes a phone uses write its name ---------------------------------------------------


def test_a_paired_devices_words_over_http_open_turns_written_under_its_name(
    tmp_path: Path,
) -> None:
    """`submit`, `submit` with a file, `ask` and `share`: the turn is the device's or the Mac's."""
    log, key, token, loopback, remote = here_daemon(tmp_path)
    phone, local = {"Authorization": f"Bearer {token}"}, {"Authorization": f"Bearer {key}"}
    file_id = remote.post(
        "/inherent/attachments", files={"file": ("a.png", PNG)}, headers=phone,
    ).json()["id"]
    routes: dict[str, tuple[str, dict[str, Any]]] = {
        "submit": ("/inherent/submit", {"text": "where am I"}),
        "submit with a file": (
            "/inherent/submit", {"text": "what is this", "attachments": [file_id]},
        ),
        "ask": ("/inherent/ask", {"text": "where am I"}),
        "share asked about": ("/inherent/share", {"text": "dinner at 7", "ask": True}),
    }
    for name, (path, body) in routes.items():
        sent = {
            "phone": remote.post(path, json=body, headers=phone),
            "mac": loopback.post(path, json=body, headers=local),
        }
        for node, response in sent.items():
            assert response.status_code == 200, (name, node, response.text)
            with closing(open_runtime_event_log(log)) as conn:
                origin = turn_origin(conn, response.json()["turn_id"])
            assert origin == ("cli_stdin", "iphone" if node == "phone" else "mac"), (name, node)
