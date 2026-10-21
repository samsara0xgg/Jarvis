"""ADR 0197: a paired phone reports what it senses in batches over HTTP.

Acceptance checks, each against the real code and a real event log:

- the strict payload check of the seven phone types, as a table of input to verdict;
- ``POST /inherent/device/events``: a batch of mixed good and bad frames is answered with one ack
  per frame in order and stores exactly the good ones, a resend writes nothing twice, the
  ``ingestion_node`` is the paired device's name, a frame whose append fails with a database error
  gets ``retry`` alone and is stored by the resend;
- the allowlists are disjoint both ways: the observers' and the playback rows' types are refused on
  the route, the phone's types on the terminal link;
- only a paired device's token opens the route: the local key on loopback is a 403, no token,
  a wrong one and a revoked one are refused, and a daemon without a brain has no route;
- the batch limits (200 frames, 1 MiB, 64 KiB a frame) and a malformed body;
- on a real server, the append runs on the loop thread that owns the log connection.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, Any, Self

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
    unpair_device,
)
from jarvis.state.event_log import EventTypeRegistry, open_event_log
from jarvis.state.phone_track import FIX_COLUMNS
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import phone_events
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.inherent_server.guards import require_local_key
from jarvis.surface.phone_events import (
    PHONE_EVENT_TYPES,
    PHONE_EVENTS_PATH,
    payload_valid,
)
from jarvis.surface.terminal_events import (
    MAX_EVENT_CHARS,
    OBSERVER_EVENT_TYPES,
    PLAYBACK_EVENT_TYPES,
    BrainEvents,
)
from tests.support.net import free_port
from tests.support.phone import (
    GOOD,
    HEALTH,
    HOUR,
    LOCATION,
    MEASURE,
    MINUTE,
    MOTION,
    NOW,
    STATE,
    TRACK,
    UUID,
    VISIT,
    event_frame,
    fixed_uid,
)
from tests.support.terminal import (
    REMOTE,
    TERMINAL_WS,
    AsRemote,
    ack_frame,
    bearer,
    brain_client,
    hello_frame,
    playback_frame,
    wait_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from httpx2 import Response  # what the test client answers with

LOOPBACK = "127.0.0.1"

MINIMAL: dict[str, dict[str, Any]] = {
    "phone.visit_observed": {"lat": 0, "lng": 0, "accuracy_m": 0, "arrived_at_ms": NOW},
    "phone.location_observed": {"lat": 0, "lng": 0, "accuracy_m": 0},
    "phone.motion_observed": {"activity": "unknown", "confidence": "low", "started_at_ms": NOW},
    "phone.health_observed": {
        "metric": "steps", "start_ms": NOW - HOUR, "end_ms": NOW, "value": 0,
    },
    "phone.state_observed": {"signal": "call", "value": "ended"},
    "phone.measure_observed": {
        "metric": "vo2_max", "start_ms": NOW, "end_ms": NOW, "value": 0, "unit": "mL/(kg*min)",
        "sample_uuid": UUID,
    },
    "phone.track_observed": {
        "started_at_ms": NOW, "ended_at_ms": NOW,
        "fixes": [[0, 0, 0, 0, None, None, None, None, None, None, 0]],
    },
}


def _changed(kind: str, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    return {**GOOD[kind], **changes}


def _without(kind: str, key: str) -> dict[str, Any]:
    return {k: v for k, v in GOOD[kind].items() if k != key}


def _span(metric: str, value: float, detail: str | None = None) -> dict[str, Any]:
    """A health span of the last hour; ``detail`` is its stage or state when given."""
    span: dict[str, Any] = {"metric": metric, "start_ms": NOW - HOUR, "end_ms": NOW, "value": value}
    return span if detail is None else {**span, "detail": detail}


# --- the payload check, as a table --------------------------------------------------------------

FIX = GOOD[TRACK]["fixes"][0]


def _track(*fixes: list[Any], start: int = NOW - HOUR) -> dict[str, Any]:
    """A track frame of ``fixes`` from ``start``; its end is the last fix's offset."""
    return {"started_at_ms": start, "ended_at_ms": start + fixes[-1][0], "fixes": list(fixes)}


def _fix(t_ms: int, **changes: Any) -> list[Any]:  # noqa: ANN401
    """The first good fix at ``t_ms``, with columns changed by name."""
    row = [t_ms, *FIX[1:]]
    for name, value in changes.items():
        row[FIX_COLUMNS.index(name)] = value
    return row

VALID: list[tuple[str, dict[str, Any]]] = [
    *((kind, payload) for kind, payload in GOOD.items()),
    *((kind, payload) for kind, payload in MINIMAL.items()),
    (LOCATION, {"lat": 90, "lng": 180, "accuracy_m": 0.0}),
    (LOCATION, {"lat": -90.0, "lng": -180.0, "accuracy_m": 1e6, "place": "p" * 200}),
    (VISIT, _changed(VISIT, departed_at_ms=GOOD[VISIT]["arrived_at_ms"])),
    (VISIT, _changed(VISIT, arrived_at_ms=NOW + 4 * MINUTE, departed_at_ms=NOW + 4 * MINUTE)),
    (HEALTH, _changed(HEALTH, detail="d" * 64)),
    (HEALTH, _changed(HEALTH, source="s" * 128, device="d" * 128, unit="u" * 16)),
    (HEALTH, _changed(HEALTH, sample_uuid=UUID.lower())),
    (HEALTH, _changed(HEALTH, meta={})),
    (HEALTH, _changed(HEALTH, meta={f"k{i}": i for i in range(12)})),
    (HEALTH, _changed(HEALTH, meta={"indoor": True, "x": -2.5, "z": "z" * 128})),
    (STATE, {"signal": "alarm", "value": "snoozed"}),
    (STATE, {"signal": "alarm", "value": "stopped"}),
    (STATE, {"signal": "power", "value": "connected"}),
    (STATE, {"signal": "power", "value": "disconnected"}),
    (STATE, {"signal": "headphones", "value": "connected", "detail": "AirPods Pro"}),
    (STATE, {"signal": "headphones", "value": "disconnected"}),
    (STATE, {"signal": "focus", "value": "off"}),
    (STATE, {"signal": "call", "value": "started"}),
    (STATE, {"signal": "battery", "value": "0"}),
    (STATE, {"signal": "battery", "value": "100", "detail": "full"}),
    (STATE, {"signal": "battery", "value": "57", "detail": "charging"}),
    (STATE, {"signal": "battery", "value": "9", "detail": "unplugged"}),
    *((MOTION, _changed(MOTION, activity=a))
      for a in ("stationary", "walking", "running", "cycling", "automotive", "unknown")),
    *((MOTION, _changed(MOTION, confidence=c)) for c in ("low", "medium", "high")),
    *((HEALTH, _changed(HEALTH, metric=m)) for m in (
        "steps", "active_energy", "basal_energy", "exercise_time", "stand_time",
        "distance_walking_running", "flights_climbed", "time_in_daylight", "workout",
        "mindful_session")),
    *((HEALTH, _span("sleep", 3, d))
      for d in ("in_bed", "awake", "unspecified", "core", "deep", "rem")),
    *((HEALTH, _span("stand_hour", 0, d)) for d in ("stood", "idle")),
    # a measure: every metric, any sign (a temperature may be below zero), a point in time
    *((MEASURE, _changed(MEASURE, metric=m)) for m in (
        "heart_rate", "resting_heart_rate", "walking_heart_rate_average",
        "heart_rate_variability_sdnn", "oxygen_saturation", "respiratory_rate",
        "sleeping_wrist_temperature", "vo2_max", "environmental_audio_exposure",
        "headphone_audio_exposure")),
    (MEASURE, _changed(MEASURE, value=-0.4)),
    (MEASURE, _changed(MEASURE, sample_uuid=UUID.lower(), source="s" * 128, unit="u" * 16)),
    # a heartbeat series: its beats are [offset ms, preceded by a gap], and value counts them
    (MEASURE, _changed(
        MEASURE, metric="heartbeat_series", value=3, beats=[[0, 1], [850, 0], [1710, 0]], part=0,
    )),
    (MEASURE, _changed(
        MEASURE, metric="heartbeat_series", value=4000, beats=[[i * 800, 0] for i in range(4000)],
    )),
    # a track: 300 fixes of a second each, ten minutes from first to last, two fixes at one moment
    (TRACK, _track(*(_fix(i * 1000) for i in range(300)))),
    (TRACK, _track(_fix(0), _fix(10 * MINUTE))),
    (TRACK, _track(_fix(0), _fix(0))),
    (TRACK, _track(_fix(0, course_deg=359.99, altitude_m=-12.5, flags=1))),
    (TRACK, _track(_fix(0, lat=-90, lng=180, speed_mps=0, flags=2))),
]
NAN, INF = float("nan"), float("inf")


INVALID: list[tuple[str, dict[str, Any]]] = [
    # an unknown key, in every type
    *((kind, {**payload, "extra": 1}) for kind, payload in GOOD.items()),
    # a required key missing
    (VISIT, _without(VISIT, "accuracy_m")),
    (VISIT, _without(VISIT, "arrived_at_ms")),
    (LOCATION, _without(LOCATION, "lng")),
    (MOTION, _without(MOTION, "confidence")),
    (HEALTH, _without(HEALTH, "value")),
    (MEASURE, _without(MEASURE, "unit")),
    (MEASURE, _without(MEASURE, "sample_uuid")),
    # a sleep span says its stage, a stand hour its state
    (HEALTH, _span("sleep", 3)),
    (HEALTH, _span("sleep", 3, "light")),
    (HEALTH, _span("sleep", 3, "REM")),
    (HEALTH, _span("stand_hour", 0)),
    (HEALTH, _span("stand_hour", 0, "sat")),
    # a heartbeat series carries its beats and nothing else does; value counts them
    (MEASURE, _changed(MEASURE, metric="heartbeat_series")),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=2, beats=[[0, 1]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=0, beats=[])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[0, 2]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[-1, 0]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[0.5, 0]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[True, 0]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[0, 0, 0]])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[0])),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[0, True]])),
    (MEASURE, _changed(
        MEASURE, metric="heartbeat_series", value=4001, beats=[[i, 0] for i in range(4001)],
    )),
    (MEASURE, _changed(
        MEASURE, metric="heartbeat_series", value=1, beats=[[0, 0]], part=1000,
    )),
    (MEASURE, _changed(MEASURE, metric="heartbeat_series", value=1, beats=[[0, 0]], part=-1)),
    (MEASURE, _changed(MEASURE, beats=[[0, 0]])),  # heart_rate has no beats
    (MEASURE, _changed(MEASURE, part=0)),
    (STATE, _without(STATE, "signal")),
    # coordinates and accuracy
    (LOCATION, _changed(LOCATION, lat=90.0001)),
    (LOCATION, _changed(LOCATION, lat=-91)),
    (LOCATION, _changed(LOCATION, lng=180.5)),
    (LOCATION, _changed(LOCATION, lng=-181)),
    (VISIT, _changed(VISIT, lat=100)),
    (LOCATION, _changed(LOCATION, accuracy_m=-1)),
    (VISIT, _changed(VISIT, accuracy_m=-0.5)),
    # a wrong type: a bool is not a number, text is not a number, null is not absent
    (LOCATION, _changed(LOCATION, lat=True)),
    (LOCATION, _changed(LOCATION, accuracy_m=False)),
    (LOCATION, _changed(LOCATION, lat="48.4")),
    (LOCATION, _changed(LOCATION, lng=[1])),
    (LOCATION, _changed(LOCATION, speed_mps=True)),
    (LOCATION, _changed(LOCATION, place=None)),
    (LOCATION, _changed(LOCATION, place=5)),
    (HEALTH, _changed(HEALTH, value=True)),
    (HEALTH, _changed(HEALTH, value="4231")),
    (HEALTH, _changed(HEALTH, detail=3)),
    (HEALTH, _changed(HEALTH, unit=3)),
    (HEALTH, _changed(HEALTH, sample_uuid=3)),
    (HEALTH, _changed(HEALTH, meta=None)),
    (HEALTH, _changed(HEALTH, meta=[])),
    (HEALTH, _changed(HEALTH, meta={"indoor": None})),  # null is not absent
    (HEALTH, _changed(HEALTH, meta={"nested": {"a": 1}})),
    (HEALTH, _changed(HEALTH, meta={"list": [1]})),
    (HEALTH, _changed(HEALTH, meta={"Upper": 1})),  # keys are lowercase words
    (HEALTH, _changed(HEALTH, meta={"": 1})),
    (HEALTH, _changed(HEALTH, meta={"k" * 65: 1})),
    (MEASURE, _changed(MEASURE, value=True)),
    (MEASURE, _changed(MEASURE, value="64")),
    (MEASURE, _changed(MEASURE, sample_uuid="not-a-uuid")),
    (MEASURE, _changed(MEASURE, unit=None)),
    (VISIT, _changed(VISIT, arrived_at_ms=True)),
    (VISIT, _changed(VISIT, arrived_at_ms=float(NOW - HOUR))),
    (MOTION, _changed(MOTION, started_at_ms=str(NOW))),
    # not finite
    (LOCATION, _changed(LOCATION, lat=NAN)),
    (LOCATION, _changed(LOCATION, lng=INF)),
    (LOCATION, _changed(LOCATION, lng=-INF)),
    (LOCATION, _changed(LOCATION, accuracy_m=INF)),
    (LOCATION, _changed(LOCATION, speed_mps=NAN)),
    (HEALTH, _changed(HEALTH, value=NAN)),
    (MEASURE, _changed(MEASURE, value=NAN)),
    (MEASURE, _changed(MEASURE, value=-INF)),
    (HEALTH, _changed(HEALTH, meta={"avg": NAN})),
    (HEALTH, _changed(HEALTH, meta={"avg": INF})),
    (HEALTH, _changed(HEALTH, value=INF)),
    # a speed or total below zero (CoreLocation's -1 means "unknown": the phone leaves it out)
    (LOCATION, _changed(LOCATION, speed_mps=-1)),
    (HEALTH, _changed(HEALTH, value=-1)),
    # outside an enum
    (MOTION, _changed(MOTION, activity="flying")),
    (MOTION, _changed(MOTION, activity="Walking")),
    (MOTION, _changed(MOTION, confidence="certain")),
    (MOTION, _changed(MOTION, confidence=2)),
    (HEALTH, _changed(HEALTH, metric="calories")),
    (HEALTH, _changed(HEALTH, metric="sleep_min")),  # the merged block is not sent any more
    (HEALTH, _changed(HEALTH, metric="workout_min")),
    (HEALTH, _changed(HEALTH, metric="heart_rate")),  # a measure, not a total
    (MEASURE, _changed(MEASURE, metric="steps")),
    (MEASURE, _changed(MEASURE, metric="sleep")),
    (STATE, {"signal": "wifi", "value": "on"}),
    (STATE, {"signal": ["focus"], "value": "on"}),
    (STATE, {"signal": "focus", "value": ["on"]}),
    (STATE, {"signal": "focus", "value": "connected"}),
    (STATE, {"signal": "alarm", "value": "on"}),
    (STATE, {"signal": "power", "value": "started"}),
    (STATE, {"signal": "headphones", "value": "on"}),
    (STATE, {"signal": "call", "value": "connected"}),
    # the battery's value is a whole percent in text, its detail one of three states
    (STATE, {"signal": "battery", "value": "101"}),
    (STATE, {"signal": "battery", "value": "-1"}),
    (STATE, {"signal": "battery", "value": "57.5"}),
    (STATE, {"signal": "battery", "value": "057"}),
    (STATE, {"signal": "battery", "value": ""}),
    (STATE, {"signal": "battery", "value": "\u0665\u0667"}),  # Arabic-Indic digits
    (STATE, {"signal": "battery", "value": 57}),
    (STATE, {"signal": "battery", "value": "connected"}),
    (STATE, {"signal": "battery", "value": "57", "detail": "Work"}),
    (STATE, {"signal": "focus", "value": "57"}),
    # a string over its cap
    (LOCATION, _changed(LOCATION, place="p" * 201)),
    (VISIT, _changed(VISIT, place="é" * 201)),
    (HEALTH, _changed(HEALTH, detail="d" * 65)),
    (HEALTH, _changed(HEALTH, source="s" * 129)),
    (HEALTH, _changed(HEALTH, device="d" * 129)),
    (HEALTH, _changed(HEALTH, unit="u" * 17)),
    (HEALTH, _changed(HEALTH, meta={f"k{i}": i for i in range(13)})),
    (HEALTH, _changed(HEALTH, meta={"z": "z" * 129})),
    (MEASURE, _changed(MEASURE, source="s" * 129)),
    (STATE, {"signal": "focus", "value": "on", "detail": "d" * 65}),
    # a span that ends before it starts
    (VISIT, _changed(VISIT, departed_at_ms=GOOD[VISIT]["arrived_at_ms"] - 1)),
    (MOTION, _changed(MOTION, ended_at_ms=GOOD[MOTION]["started_at_ms"] - 1)),
    (HEALTH, _changed(HEALTH, end_ms=GOOD[HEALTH]["start_ms"] - 1)),
    (MEASURE, _changed(MEASURE, end_ms=GOOD[MEASURE]["start_ms"] - 1)),
    # a time that is negative or later than the brain's clock by more than five minutes
    (VISIT, _changed(VISIT, arrived_at_ms=-1)),
    (VISIT, _changed(VISIT, arrived_at_ms=NOW + 6 * MINUTE, departed_at_ms=NOW + 6 * MINUTE)),
    (VISIT, _changed(VISIT, departed_at_ms=64_092_211_200_000)),  # CLVisit's "distantFuture"
    (MOTION, _changed(MOTION, ended_at_ms=NOW + 10 * MINUTE)),
    (HEALTH, _changed(HEALTH, end_ms=NOW + HOUR)),
    (MEASURE, _changed(MEASURE, start_ms=NOW + HOUR, end_ms=NOW + HOUR)),
    # a track's fixes: rows of eleven, in time order, from its start to its end, ten minutes at most
    (TRACK, _changed(TRACK, fixes=[])),
    (TRACK, _changed(TRACK, fixes=FIX)),
    (TRACK, _track(*(_fix(i * 1000) for i in range(301)))),
    (TRACK, _track(_fix(0), _fix(10 * MINUTE + 1))),
    (TRACK, _track(_fix(0), _fix(2000), _fix(1000))),
    (TRACK, _changed(TRACK, ended_at_ms=GOOD[TRACK]["ended_at_ms"] + 1)),
    (TRACK, _changed(TRACK, started_at_ms=GOOD[TRACK]["started_at_ms"] - 1)),
    (TRACK, _track(_fix(1000))),
    (TRACK, _track(_fix(0)[:10])),
    (TRACK, _track([*_fix(0), 0])),
    (TRACK, _track(_fix(-1))),
    (TRACK, _changed(TRACK, fixes=[_fix(0), [1.5, *FIX[1:]]])),
    (TRACK, _track(_fix(0, lat=90.1))),
    (TRACK, _track(_fix(0, lat=None))),
    (TRACK, _track(_fix(0, lng=True))),
    (TRACK, _track(_fix(0, accuracy_m=None))),
    (TRACK, _track(_fix(0, accuracy_m=-1))),
    (TRACK, _track(_fix(0, altitude_m=NAN))),
    (TRACK, _track(_fix(0, vertical_accuracy_m=-1))),
    (TRACK, _track(_fix(0, speed_mps=-1))),  # CoreLocation's "unknown" travels as null
    (TRACK, _track(_fix(0, speed_accuracy_mps=INF))),
    (TRACK, _track(_fix(0, course_deg=360))),
    (TRACK, _track(_fix(0, course_deg=-1))),
    (TRACK, _track(_fix(0, course_accuracy_deg="4"))),
    (TRACK, _track(_fix(0, flags=4))),
    (TRACK, _track(_fix(0, flags=True))),
    (TRACK, _track(_fix(0, flags=None))),
    (TRACK, _changed(TRACK, ended_at_ms=GOOD[TRACK]["started_at_ms"] - 1)),
    (TRACK, _track(_fix(0), start=NOW + 6 * MINUTE)),
]


@pytest.mark.parametrize(("kind", "payload"), VALID)
def test_a_payload_that_is_exactly_what_the_phone_type_carries_is_valid(
    kind: str, payload: dict[str, Any],
) -> None:
    """Every field and enum of the seven types, at its edges."""
    assert payload_valid(kind, payload, NOW)


@pytest.mark.parametrize(("kind", "payload"), INVALID)
def test_a_payload_the_phone_type_does_not_carry_is_refused(
    kind: str, payload: dict[str, Any],
) -> None:
    """Unknown keys, wrong types, non-finite or out-of-range numbers, enums, caps, spans, clock."""
    assert not payload_valid(kind, payload, NOW)


def test_only_the_seven_phone_types_have_a_check_and_the_registry_names_the_same_fields() -> None:
    """The check knows each registered field and no other, and a non-phone type is not valid."""
    assert set(GOOD) == PHONE_EVENT_TYPES
    for kind in PHONE_EVENT_TYPES:
        schema = EventTypeRegistry.get(kind)
        assert schema is not None
        assert (schema.owner_layer, schema.actor) == ("L5", "observer")
        assert set(phone_events._CHECKS[kind]) == {  # noqa: SLF001
            *schema.required_payload, *schema.optional_payload,
        }
        # every required field is needed: leaving any one out is refused
        for key in schema.required_payload:
            assert not payload_valid(kind, _without(kind, key), NOW)
    assert not payload_valid("repo.state_observed", {}, NOW)
    assert not payload_valid("phone.unknown", {}, NOW)
    assert not PHONE_EVENT_TYPES & (OBSERVER_EVENT_TYPES | PLAYBACK_EVENT_TYPES)


# --- the route -------------------------------------------------------------------------------


def _rows(log: Path) -> list[tuple[str, str, int, dict[str, Any], str, str]]:
    """The phone rows of the brain's log: uid, type, time, payload, actor, ingestion node."""
    with sqlite3.connect(log) as conn:
        return [
            (uid, kind, ts, json.loads(payload), actor, node)
            for uid, kind, ts, payload, actor, node in conn.execute(
                "SELECT event_uid, type, ts_epoch_ms, payload_json, actor, ingestion_node "
                "FROM events WHERE type LIKE 'phone.%' ORDER BY id",
            )
        ]


def _post(
    client: TestClient, token: str, frames: list[Any],
) -> Response:
    return client.post(PHONE_EVENTS_PATH, json={"events": frames}, headers=bearer(token))


def _ok(n: int) -> dict[str, Any]:
    return {"type": "ack", "event_uid": fixed_uid(n), "ok": True}


def _refused(uid: str | None, code: str) -> dict[str, Any]:
    return {"type": "ack", "event_uid": uid, "ok": False, "code": code}


def test_a_heartbeat_series_at_the_largest_size_is_stored_whole_and_a_larger_one_is_refused(
    tmp_path: Path,
) -> None:
    """4000 beats of the longest offsets fit one frame's 64 KiB; more is a ``bad_event``."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    beats = [[86_399_000 + i, i % 2] for i in range(4000)]
    series = {**GOOD[MEASURE], "metric": "heartbeat_series", "value": 4000, "beats": beats,
              "part": 0}
    too_many = {**series, "value": 4001, "beats": [*beats, [86_400_000, 0]]}

    reply = _post(
        client, token, [event_frame(1, MEASURE, series), event_frame(2, MEASURE, too_many)]
    )

    assert reply.json() == {"acks": [_ok(1), _refused(fixed_uid(2), "bad_event")]}
    [(uid, kind, _ts, stored, actor, _node)] = _rows(log)
    assert (uid, kind, actor) == (fixed_uid(1), MEASURE, "observer")
    assert stored == series


def test_a_mixed_batch_is_answered_one_ack_per_frame_in_order_and_stores_only_the_good_ones(
    tmp_path: Path,
) -> None:
    """Seven good frames among fourteen refused, each for its own code; those leave no row."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    old = NOW - 10 * 24 * HOUR
    batch: list[Any] = [
        event_frame(1, VISIT),
        event_frame(2, LOCATION, _changed(LOCATION, lat=91)),
        event_frame(3, LOCATION),
        event_frame(4, MOTION),
        event_frame(5, "repo.state_observed", {"repo_path": "/r"}),
        event_frame(6, HEALTH),
        event_frame(7, STATE, {"signal": "focus", "value": "connected"}),
        event_frame(8, "phone.sleeping", {}),
        event_frame(9, STATE),
        "not a frame",
        event_frame(10, LOCATION, _changed(LOCATION, place="x" * 201)),
        {**event_frame(11, LOCATION), "source_event_id": fixed_uid(1)},
        {**event_frame(12, LOCATION), "correlation": {"turn_id": "T"}},
        {**event_frame(13, LOCATION), "schema_version": 1},
        {**event_frame(14, LOCATION), "ts_epoch_ms": "now"},
        {**event_frame(15, LOCATION), "event_uid": "NOT-A-UID"},
        {**event_frame(16, LOCATION), "ts_epoch_ms": old},
        {**event_frame(17, LOCATION), "type": "event"},
        {**event_frame(18, LOCATION), "type": "tts"},
        event_frame(19, "surface.playback_completed", playback_frame(fixed_uid(19))["payload"]),
        event_frame(20, LOCATION, {"x": "a" * MAX_EVENT_CHARS}),
    ]
    reply = _post(client, token, batch)

    assert reply.status_code == 200
    assert reply.json() == {
        "acks": [
            _ok(1),
            _refused(fixed_uid(2), "bad_event"),
            _ok(3),
            _ok(4),
            _refused(fixed_uid(5), "event_type_not_allowed"),
            _ok(6),
            _refused(fixed_uid(7), "bad_event"),
            _refused(fixed_uid(8), "event_type_not_allowed"),
            _ok(9),
            _refused(None, "bad_event"),
            _refused(fixed_uid(10), "bad_event"),
            _refused(fixed_uid(11), "bad_event"),
            _refused(fixed_uid(12), "bad_event"),
            _refused(fixed_uid(13), "bad_event"),
            _refused(fixed_uid(14), "bad_event"),
            _refused(None, "bad_event"),
            _ok(16),
            _ok(17),
            _refused(fixed_uid(18), "bad_event"),
            _refused(fixed_uid(19), "event_type_not_allowed"),
            _refused(fixed_uid(20), "event_too_large"),
        ]
    }
    rows = _rows(log)
    assert [(uid, kind) for uid, kind, *_ in rows] == [
        (fixed_uid(1), VISIT),
        (fixed_uid(3), LOCATION),
        (fixed_uid(4), MOTION),
        (fixed_uid(6), HEALTH),
        (fixed_uid(9), STATE),
        (fixed_uid(16), LOCATION),
        (fixed_uid(17), LOCATION),
    ]
    by_uid = {uid: (ts, payload, actor, node) for uid, _kind, ts, payload, actor, node in rows}
    ts, payload, actor, node = by_uid[fixed_uid(1)]
    assert (ts, payload, actor, node) == (NOW, GOOD[VISIT], "observer", "phone")
    # the fix time of a location is the event's own time; a clock a week off is not trusted
    assert by_uid[fixed_uid(3)][0] == NOW
    assert abs(by_uid[fixed_uid(16)][0] - int(time.time() * 1000)) < 60_000
    assert {node for *_rest, node in by_uid.values()} == {"phone"}


def test_a_resent_batch_is_acknowledged_again_and_writes_nothing_twice(tmp_path: Path) -> None:
    """The phone lost the answer and sends the same frames; and one frame twice in one batch."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    batch = [event_frame(1, VISIT), event_frame(2, LOCATION), event_frame(3, STATE)]
    first = _post(client, token, batch).json()
    assert first == {"acks": [_ok(1), _ok(2), _ok(3)]}
    assert len(_rows(log)) == 3
    assert _post(client, token, batch).json() == first
    assert _post(client, token, [batch[1], batch[1], event_frame(4, MOTION)]).json() == {
        "acks": [_ok(2), _ok(2), _ok(4)],
    }
    assert [uid for uid, *_ in _rows(log)] == [fixed_uid(n) for n in (1, 2, 3, 4)]


def test_each_device_writes_under_its_own_name(tmp_path: Path) -> None:
    """The name comes from the token alone; nothing in the frame can change it."""
    phone, spare = pair_device(tmp_path, "iphone"), pair_device(tmp_path, "ipad.old")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    assert _post(client, phone, [event_frame(1, LOCATION)]).json() == {"acks": [_ok(1)]}
    assert _post(client, spare, [event_frame(2, LOCATION)]).json() == {"acks": [_ok(2)]}
    liar = {**event_frame(3, LOCATION), "ingestion_node": "macbook", "device": "macbook"}
    assert _post(client, phone, [liar]).json() == {"acks": [_refused(fixed_uid(3), "bad_event")]}
    assert [(uid, node) for uid, *_mid, node in _rows(log)] == [
        (fixed_uid(1), "iphone"),
        (fixed_uid(2), "ipad.old"),
    ]


def test_a_database_error_on_one_frame_is_retry_for_that_frame_and_the_resend_stores_it(
    tmp_path: Path,
) -> None:
    """The append of one uid fails inside SQLite; its neighbours are stored and the retry lands."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    with sqlite3.connect(log) as conn:
        conn.execute(
            "CREATE TRIGGER fail_one BEFORE INSERT ON events "
            f"WHEN NEW.event_uid = '{fixed_uid(2)}' BEGIN SELECT RAISE(ABORT, 'disk is full'); END",
        )
    batch = [
        event_frame(1, LOCATION),
        event_frame(2, LOCATION),
        event_frame(3, STATE),
        event_frame(4, LOCATION, {}),
    ]
    assert _post(client, token, batch).json() == {
        "acks": [
            _ok(1),
            _refused(fixed_uid(2), "retry"),
            _ok(3),
            _refused(fixed_uid(4), "bad_event"),
        ]
    }
    assert [uid for uid, *_ in _rows(log)] == [fixed_uid(1), fixed_uid(3)]

    with sqlite3.connect(log) as conn:
        conn.execute("DROP TRIGGER fail_one")
    # the phone resends only what was `retry`
    assert _post(client, token, [batch[1]]).json() == {"acks": [_ok(2)]}
    assert [uid for uid, *_ in _rows(log)] == [fixed_uid(1), fixed_uid(3), fixed_uid(2)]


# --- the allowlists, both ways -------------------------------------------------------------------


def test_the_route_refuses_the_observers_and_playback_types_and_the_link_refuses_the_phones(
    tmp_path: Path,
) -> None:
    """Each sender writes only its own types; the refusal is the same code on both transports."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    foreign = [
        {"event_uid": fixed_uid(100 + i), "event_type": kind, "ts_epoch_ms": NOW, "payload": {}}
        for i, kind in enumerate(sorted(OBSERVER_EVENT_TYPES | PLAYBACK_EVENT_TYPES))
    ]
    acks = _post(client, token, foreign).json()["acks"]
    assert [ack["code"] for ack in acks] == ["event_type_not_allowed"] * len(foreign)
    assert [ack["event_uid"] for ack in acks] == [frame["event_uid"] for frame in foreign]

    with client.websocket_connect(TERMINAL_WS, headers=bearer(token)) as ws:
        ws.send_text(hello_frame(voice=True))  # even a voice terminal's list has no phone type
        ws.receive_json()
        for i, kind in enumerate(sorted(PHONE_EVENT_TYPES)):
            frame = {
                "type": "event",
                "event_uid": fixed_uid(200 + i),
                "event_type": kind,
                "ts_epoch_ms": NOW,
                "payload": GOOD[kind],
            }
            assert ack_frame(ws, frame) == _refused(fixed_uid(200 + i), "event_type_not_allowed")
    assert _rows(log) == []
    with sqlite3.connect(log) as conn:
        left = conn.execute("SELECT COUNT(*) FROM events WHERE event_uid >= ?", (fixed_uid(100),))
        assert left.fetchone() == (0,)


# --- who may call it ----------------------------------------------------------------------------


def _loopback(client: TestClient) -> TestClient:
    """The same app, called from this machine."""
    return TestClient(client.app, base_url="http://127.0.0.1:8006", client=(LOOPBACK, 50000))


def test_the_local_key_alone_is_a_403_and_only_a_paired_devices_token_opens_the_route(
    tmp_path: Path,
) -> None:
    """On loopback the key passes the guard and the route turns it down; elsewhere the guard does.

    A device token on loopback is the guard's 401, as on the terminal link.
    """
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    local = _loopback(client)
    body = {"events": [event_frame(1, LOCATION)]}

    keyed = local.post(PHONE_EVENTS_PATH, json=body, headers=bearer(local_key(tmp_path)))
    assert (keyed.status_code, keyed.json()) == (
        403,
        {"detail": "a paired device's token is required"},
    )
    assert local.post(PHONE_EVENTS_PATH, json=body).status_code == 401
    # a device token is not the local key: on loopback the guard checks the key
    assert local.post(PHONE_EVENTS_PATH, json=body, headers=bearer(token)).status_code == 401

    assert client.post(PHONE_EVENTS_PATH, json=body).status_code == 401
    assert client.post(PHONE_EVENTS_PATH, json=body, headers=bearer("wrong")).status_code == 401
    assert (
        client.post(
            PHONE_EVENTS_PATH,
            json=body,
            headers=bearer(local_key(tmp_path)),
        ).status_code
        == 401
    )
    assert (
        client.post(
            PHONE_EVENTS_PATH,
            json=body,
            headers={"Authorization": token},
        ).status_code
        == 401
    )
    assert _rows(log) == []

    assert _post(client, token, body["events"]).status_code == 200
    unpair_device(tmp_path, "phone")
    assert _post(client, token, [event_frame(2, LOCATION)]).status_code == 401
    assert [uid for uid, *_ in _rows(log)] == [fixed_uid(1)]


def test_a_daemon_that_does_not_listen_has_no_such_route(tmp_path: Path) -> None:
    """No log to append to, or no device tokens, no route: 404 to a holder of every credential."""
    token = pair_device(tmp_path, "phone")
    events_only = (BrainEvents(sqlite3.connect(":memory:")), None)
    for events, name in ((None, lambda _t: "phone"), events_only):
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone_events=events,
                device_name=name,
            )
        )
        require_local_key(
            app,
            lambda _h: True,
            device_token_matches=lambda _t: True,
        )
        client = TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000))
        assert _post(client, token, []).status_code == 404


# --- the batch limits ------------------------------------------------------------------------


def test_the_batch_has_limits_and_a_malformed_body_is_a_400(tmp_path: Path) -> None:
    """200 frames and 1 MiB a body, declared or streamed; anything else is not a batch."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    headers = {**bearer(token), "Content-Type": "application/json"}
    url = PHONE_EVENTS_PATH

    assert _post(client, token, []).json() == {"acks": []}
    full = _post(client, token, [event_frame(n, LOCATION) for n in range(200)])
    assert full.status_code == 200
    assert [ack["ok"] for ack in full.json()["acks"]] == [True] * 200
    over = _post(client, token, [event_frame(n, LOCATION) for n in range(200, 401)])
    assert over.status_code == 413
    assert len(_rows(log)) == 200  # an over-long batch is refused whole

    pad = json.dumps({"events": [], "pad": "x" * (1024 * 1024)})
    assert client.post(url, content=pad, headers=headers).status_code == 413
    chunks = (pad[i : i + 4096].encode() for i in range(0, len(pad), 4096))
    assert client.post(url, content=chunks, headers=headers).status_code == 413
    exact = json.dumps({"events": []}).ljust(1024 * 1024)
    assert client.post(url, content=exact, headers=headers).json() == {"acks": []}

    for content in (
        "",
        "[]",
        '{"events": {}}',
        '{"events": "x"}',
        "{",
        '{"event": []}',
        "null",
        b"\xff\xfe",
        "[" * 100_000,
    ):
        assert client.post(url, content=content, headers=headers).status_code == 400, content


def test_a_frame_over_the_64_kib_cap_is_refused_alone(tmp_path: Path) -> None:
    """The cap is the link's: a big frame is `event_too_large` and its neighbours are stored."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    big = event_frame(2, LOCATION, {"place": "x" * MAX_EVENT_CHARS})
    acks = _post(client, token, [event_frame(1, LOCATION), big, event_frame(3, LOCATION)]).json()[
        "acks"
    ]
    assert acks == [_ok(1), _refused(fixed_uid(2), "event_too_large"), _ok(3)]
    assert [uid for uid, *_ in _rows(log)] == [fixed_uid(1), fixed_uid(3)]


def test_nan_and_infinity_sent_as_json_literals_are_bad_events(tmp_path: Path) -> None:
    """Python's JSON reads `NaN`; the check does not let it into the log."""
    token = pair_device(tmp_path, "phone")
    client, _hub, log = brain_client(tmp_path, speaks=False)
    head = f'{{"event_type": "phone.location_observed", "ts_epoch_ms": {NOW}, '
    text = (
        f'{{"events": [{head}"event_uid": "{fixed_uid(1)}", '
        '"payload": {"lat": NaN, "lng": 1, "accuracy_m": 1}}, '
        f'{head}"event_uid": "{fixed_uid(2)}", '
        '"payload": {"lat": 1, "lng": 1, "accuracy_m": Infinity}}]}'
    )
    reply = client.post(
        PHONE_EVENTS_PATH,
        content=text,
        headers={**bearer(token), "Content-Type": "application/json"},
    )
    assert reply.json() == {
        "acks": [_refused(fixed_uid(1), "bad_event"), _refused(fixed_uid(2), "bad_event")],
    }
    assert _rows(log) == []


# --- a real server: the append is on the loop thread that owns the connection -----------------


class _LoopOwnedBrain:
    """A real uvicorn server whose log connection can be used only by the server's loop thread."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.port = free_port()
        self.log = root / "events.db"
        open_event_log(self.log).close()
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._serve()), daemon=True)

    async def _serve(self) -> None:
        conn = open_event_log(self.log)  # check_same_thread stays on: this thread only
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone_events=BrainEvents(conn),
                device_name=lambda token: device_name_for_token(self.root, token),
            )
        )
        require_local_key(
            app,
            lambda header: local_key_matches(local_key(self.root), header),
            device_token_matches=lambda token: device_token_matches(self.root, token),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                AsRemote(app),
                host=LOOPBACK,
                port=self.port,
                log_level="warning",
                lifespan="off",
            )
        )
        await self.server.serve()
        conn.close()

    def __enter__(self) -> Self:
        self.thread.start()
        wait_for(lambda: self.server is not None and self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}{PHONE_EVENTS_PATH}"


def test_on_a_real_server_the_batch_is_appended_on_the_loop_that_owns_the_log(
    tmp_path: Path,
) -> None:
    """A route run on a worker thread would fail on this connection; this one stores the batch."""
    token = pair_device(tmp_path, "phone")
    with _LoopOwnedBrain(tmp_path) as brain:
        reply = httpx.post(
            brain.url,
            json={"events": [event_frame(1, VISIT), event_frame(2, LOCATION)]},
            headers=bearer(token),
            timeout=10,
        )
        assert (reply.status_code, reply.json()) == (200, {"acks": [_ok(1), _ok(2)]})
        resent = httpx.post(
            brain.url,
            json={"events": [event_frame(2, LOCATION)]},
            headers=bearer(token),
            timeout=10,
        )
        assert resent.json() == {"acks": [_ok(2)]}
        assert httpx.post(brain.url, json={"events": []}, timeout=10).status_code == 401
    assert [(uid, node) for uid, _k, _t, _p, _a, node in _rows(brain.log)] == [
        (fixed_uid(1), "phone"),
        (fixed_uid(2), "phone"),
    ]
