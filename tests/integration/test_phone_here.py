"""ADR 0198: on a brain, "here" is the phone's latest report, read from the log when asked.

Acceptance checks, each against a real event log:

- which report is the latest, as a table of log rows to the reading: a location at its event time,
  a visit at its departure (else its arrival), the latest time wins, the later row on a tie, any
  device, no age cutoff, no report at all;
- the whole path: a batch posted to ``/inherent/device/events`` is what ``here`` reads;
- on a brain booted for real, `where_am_i` and `transit`'s `here` say the fix is the phone's last
  report with its age and accuracy, read from a tool's own thread; with no report, or a log that
  cannot be read, they are a tool error that tells the model to ask;
- the age in words, and a Mac running alone keeping the wording of ADR 0194.
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import time
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.commentary import _dispatch_key
from jarvis.execution import transit_tool
from jarvis.execution.location_tool import describe_age
from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.runtime import _phone_here, bootstrap_runtime_app
from jarvis.shared import CallerPrincipal
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.phone_location import NoPhoneReport, read_phone_here
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from tests.integration.test_phone_events import GOOD, LOCATION, VISIT, _frame
from tests.integration.test_terminal_voice import _bearer, _brain_client
from tests.integration.test_transit_tool import _serve

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


def reading(
    lat: float, lng: float, acc: float, age_s: int, device: str = "phone", **place: str,
) -> dict[str, Any]:
    """What ``read_phone_here`` returns for a report like this."""
    return {"lat": lat, "lng": lng, "accuracy_m": acc, "age_s": age_s, "source": "phone",
            "device": device, **place}


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
        [loc(T - 90_000, 48.46, -123.31)], reading(48.46, -123.31, 50.0, 90),
    ),
    "a location keeps its place name": (
        [loc(T - 5 * MIN, 48.46, -123.31, acc=120, **UVIC)],
        reading(48.46, -123.31, 120.0, 300, **UVIC),
    ),
    "a visit without a departure is at its arrival": (
        [visit(T - 2 * MIN, T - 3 * HOUR, None, 48.4, -123.4, **UVIC)],
        reading(48.4, -123.4, 65.0, 3 * 3600, **UVIC),
    ),
    "a visit with a departure is at its departure": (
        [visit(T - 2 * MIN, T - 3 * HOUR, T - HOUR, 48.4, -123.4)],
        reading(48.4, -123.4, 65.0, 3600),
    ),
    "a location after a visit's arrival is later": (
        [visit(T - HOUR, T - 3 * HOUR, None, 48.4, -123.4), loc(T - 10 * MIN, 48.5, -123.5)],
        reading(48.5, -123.5, 50.0, 600),
    ),
    "a visit's arrival after the last location is later": (
        [loc(T - HOUR, 48.5, -123.5), visit(T - 20 * MIN, T - 30 * MIN, None, 48.4, -123.4)],
        reading(48.4, -123.4, 65.0, 30 * 60),
    ),
    "a departure after the last location is later, though its row is older": (
        [visit(T - 40 * MIN, T - 3 * HOUR, T - 45 * MIN, 48.4, -123.4),
         loc(T - 50 * MIN, 48.5, -123.5)],
        reading(48.4, -123.4, 65.0, 45 * 60),
    ),
    "a location after a visit's departure is later, though its row is older": (
        [loc(T - 20 * MIN, 48.5, -123.5),
         visit(T - 10 * MIN, T - 3 * HOUR, T - 25 * MIN, 48.4, -123.4)],
        reading(48.5, -123.5, 50.0, 20 * 60),
    ),
    "a tie goes to the later row, a location after a visit": (
        [visit(T - 8 * MIN, T - 10 * MIN, None, 48.4, -123.4), loc(T - 10 * MIN, 48.5, -123.5)],
        reading(48.5, -123.5, 50.0, 600),
    ),
    "a tie goes to the later row, a visit after a location": (
        [loc(T - 10 * MIN, 48.5, -123.5), visit(T - 8 * MIN, T - 10 * MIN, None, 48.4, -123.4)],
        reading(48.4, -123.4, 65.0, 600),
    ),
    "a tie goes to the later row, a departure and a location": (
        [loc(T - 10 * MIN, 48.5, -123.5),
         visit(T - 8 * MIN, T - HOUR, T - 10 * MIN, 48.4, -123.4)],
        reading(48.4, -123.4, 65.0, 600),
    ),
    "a tie between two locations goes to the later row": (
        [loc(T - MIN, 48.5, -123.5), loc(T - MIN, 48.6, -123.6)],
        reading(48.6, -123.6, 50.0, 60),
    ),
    "the latest report of any device wins and names its device": (
        [loc(T - 30 * MIN, 48.5, -123.5, device="iphone"),
         loc(T - 10 * MIN, 48.6, -123.6, device="ipad"),
         loc(T - 20 * MIN, 48.7, -123.7, device="iphone")],
        reading(48.6, -123.6, 50.0, 600, device="ipad"),
    ),
    "a report ten days old is still where he is": (
        [loc(T - 10 * DAY, 48.5, -123.5)], reading(48.5, -123.5, 50.0, 10 * 24 * 3600),
    ),
    "a time ahead of the clock is no age": (
        [loc(T + 2 * MIN, 48.5, -123.5)], reading(48.5, -123.5, 50.0, 0),
    ),
    "the age is whole seconds, rounded down": (
        [loc(T - 1_999, 48.5, -123.5)], reading(48.5, -123.5, 50.0, 1),
    ),
    "the noise around a report changes nothing": (
        [*NOISE, loc(T - 7 * MIN, 48.5, -123.5), *NOISE], reading(48.5, -123.5, 50.0, 420),
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_the_latest_report_is_the_reading_with_its_age(name: str, tmp_path: Path) -> None:
    """Log rows in, the reading out: position, accuracy, place if any, age, source and device."""
    rows, expected = CASES[name]
    conn = write(tmp_path / "events.db", rows)
    try:
        if expected is None:
            with pytest.raises(NoPhoneReport, match="no phone has reported a location"):
                read_phone_here(conn, now_ms=T)
        else:
            assert read_phone_here(conn, now_ms=T) == expected
    finally:
        conn.close()


def test_a_batch_posted_to_the_route_is_what_here_reads(tmp_path: Path) -> None:
    """Phone to brain to `here`: a visit, then a newer location, through the real route and log."""
    token = pair_device(tmp_path, "iphone")
    client, _hub, log = _brain_client(tmp_path, speaks=False)
    now = int(time.time() * 1000)
    visit_payload = {**GOOD[VISIT], "arrived_at_ms": now - 3 * HOUR, "departed_at_ms": now - HOUR}
    fix_payload = {**GOOD[LOCATION], "lat": 48.43, "lng": -123.37, "accuracy_m": 80}
    batch = [
        {**_frame(1, VISIT, visit_payload), "ts_epoch_ms": now - HOUR},
        {**_frame(2, LOCATION, fix_payload), "ts_epoch_ms": now - 20 * MIN},
    ]
    posted = client.post(PHONE_EVENTS_PATH, json={"events": batch}, headers=_bearer(token))
    assert [ack["ok"] for ack in posted.json()["acks"]] == [True, True]
    with sqlite3.connect(log) as conn:
        here = read_phone_here(conn)
    assert here["device"] == "iphone"
    assert (here["lat"], here["lng"], here["accuracy_m"]) == (48.43, -123.37, 80.0)
    assert here["place"] == fix_payload["place"]
    assert 20 * 60 <= here["age_s"] <= 20 * 60 + 30


# --- on a brain, the tools --------------------------------------------------------------------


def _brain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JarvisRuntime:
    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key-not-real")
    root = tmp_path / "root"
    root.mkdir()
    (root / "settings.yaml").write_text("runtime:\n  role: brain\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    return bootstrap_runtime_app(runtime_root=root)


def _tool(runtime: JarvisRuntime, name: str) -> Any:  # noqa: ANN401
    (definition,) = (d for d in runtime.tool_registry.get_definitions() if d.name == name)
    return definition


def _report(runtime: JarvisRuntime, kind: str, payload: dict[str, Any], ts: int) -> None:
    """A phone's report through the brain's own hub, as the route hands it over."""
    assert runtime.terminal_hub is not None
    assert runtime.terminal_hub.events is not None
    frame = {"event_uid": f"{int(time.time_ns()):032x}", "event_type": kind,
             "ts_epoch_ms": ts, "payload": payload}
    [ack] = runtime.terminal_hub.events.record_phone_batch("iphone", [frame])
    assert ack["ok"] is True, ack


def _on_a_tool_thread(
    event_log: Path, definition: Any, args: dict[str, Any],  # noqa: ANN401
) -> dict[str, Any]:
    """Run a handler on a worker thread, as a tool runs: the runtime's connection can't go there."""

    def run() -> dict[str, Any]:
        conn = sqlite3.connect(event_log)
        try:  # The transit card (ADR 0205) is written through the call's own connection.
            ctx = SimpleNamespace(conn=conn, action_id="A1")
            return dict(definition.handler(args, ctx))
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(run).result()


def test_on_a_brain_where_am_i_and_transit_here_say_it_is_the_phones_last_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both tools exist on a brain on any machine and read the log from a tool's own thread.

    They carry the report's age and accuracy; transit starts the trip from the reported position.
    """
    seen = _serve(monkeypatch)
    # A direct call has no `action.running` event to link the transit card to.
    monkeypatch.setattr(transit_tool, "_get_running_event_uid", lambda *_: None)
    brain = _brain(tmp_path, monkeypatch)
    try:
        where, transit = _tool(brain, "where_am_i"), _tool(brain, "transit")
        for definition in (where, transit):
            assert "phone" in definition.description
            assert "500 m" in definition.description
            assert "Mac" not in definition.description
        assert where.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
        assert where.read_only
        assert not where.requires_confirmation
        assert _dispatch_key("where_am_i") is None

        call = partial(_on_a_tool_thread, brain.runtime_paths.event_log)

        with pytest.raises(ToolError, match=r"no phone has reported a location.*ask the user"):
            call(where, {})
        with pytest.raises(ToolError, match="no phone has reported a location") as error:
            call(transit, {"origin": "here", "destination": "Mayfair Mall"})
        assert error.value.code == "location_unavailable"
        assert seen == []

        now = int(time.time() * 1000)
        _report(brain, "phone.location_observed", {**GOOD[LOCATION], "accuracy_m": 120.4},
                now - 12 * MIN)
        got = call(where, {})
        assert got["lat"] == GOOD[LOCATION]["lat"]
        assert got["lng"] == GOOD[LOCATION]["lng"]
        assert got["accuracy_m"] == 120.4
        assert got["place"] == GOOD[LOCATION]["place"]
        assert (got["source"], got["device"]) == ("phone", "iphone")
        assert 12 * 60 <= got["age_s"] <= 12 * 60 + 30
        assert "the phone's last report 12 min ago, accurate to about 120 m" in got["note"]
        assert "has not moved far since" in got["note"]

        trip = call(transit, {"origin": "here", "destination": "Mayfair Mall"})
        place = GOOD[LOCATION]["place"]
        assert trip["from"] == f"here (the phone's last report 12 min ago, {place}, ±120 m)"
        assert trip["to"] == "Mayfair Mall"
        (request, _timeout) = seen[0]
        origin = json.loads(request.data)["origin"]["location"]["latLng"]
        assert origin == {"latitude": GOOD[LOCATION]["lat"], "longitude": GOOD[LOCATION]["lng"]}
        assert trip["options"]
    finally:
        brain.conn.close()


def test_a_log_that_cannot_be_read_is_a_tool_error_that_says_to_ask(tmp_path: Path) -> None:
    """The reader is the runtime's own: a missing log is the phone's report not being readable."""
    registry = build_default_registry(
        here_location=partial(_phone_here, tmp_path / "missing" / "events.db"),
        here_from_phone=True,
    )
    definition = next(d for d in registry.get_definitions() if d.name == "where_am_i")
    unreadable = r"last location report could not be read.*ask the user"
    with pytest.raises(ToolError, match=unreadable) as e:
        definition.handler({}, None)  # type: ignore[arg-type,call-arg,misc]
    assert e.value.code == "location_unavailable"


def test_a_mac_running_alone_keeps_the_wording_of_adr_0194() -> None:
    """Without the phone flag the description and the label are the Mac's, as before."""
    fix = {"lat": 48.46, "lng": -123.31, "accuracy_m": 40.4, "place": "Ring Rd, Saanich"}
    registry = build_default_registry(
        transit_api_key="k", transit_places={}, here_location=lambda: fix,
    )
    where = next(d for d in registry.get_definitions() if d.name == "where_am_i")
    transit = next(d for d in registry.get_definitions() if d.name == "transit")
    assert "read from this Mac (he carries it)" in where.description
    assert "his location, read from this Mac, which he carries." in transit.description
    assert "phone" not in where.description + transit.description
    mac_reading = dict(where.handler({}, None))  # type: ignore[arg-type,call-arg,misc]
    assert mac_reading["source"] == "this Mac"


@pytest.mark.parametrize(("seconds", "words"), [
    (0, "under a minute"), (59, "under a minute"), (60, "1 min"), (719, "11 min"),
    (3599, "59 min"), (3600, "1 h"), (3660, "1 h 1 min"), (86_399, "23 h 59 min"),
    (86_400, "1 d"), (90_000, "1 d 1 h"), (864_000, "10 d"),
])
def test_the_age_is_told_in_words(seconds: int, words: str) -> None:
    """What the model reads for how old a report is."""
    assert describe_age(seconds) == words
