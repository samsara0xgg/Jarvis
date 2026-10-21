"""The phone's reports (ADR 0197): ``POST /inherent/device/events`` and ``contracts/phone_events``.

A paired phone sends its sensed events as a batch of frames and the brain answers one ack per
frame. The files hold what the phone may send, as ``valid`` and ``invalid`` lists the iOS tests
encode against, and what the brain answers, as examples produced here by the real route over a
real event log: the brain must take every valid case and refuse every invalid one with the code
the file names, and the acks it sends must keep the shape the phone decodes.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.device_tokens import pair_device
from jarvis.state.phone_track import FIX_COLUMNS
from jarvis.surface import phone_events
from jarvis.surface.phone_events import PHONE_EVENTS_PATH
from jarvis.surface.terminal_events import MAX_EVENT_CHARS
from tests.support.contracts import WORLD_NOW_MS, Case, Contracts, brain_at
from tests.support.phone import fixed_uid
from tests.support.terminal import bearer, brain_client

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi.testclient import TestClient

ROUTE = "POST /inherent/device/events"

# the contract of each phone type: its event type, and the lists of the brain that its file names
TYPES: dict[str, tuple[str, dict[str, tuple[str, ...]]]] = {
    "phone_events/visit": ("phone.visit_observed", {}),
    "phone_events/location": ("phone.location_observed", {}),
    "phone_events/motion": (
        "phone.motion_observed",
        {"activities": phone_events.ACTIVITIES, "confidences": phone_events.CONFIDENCES},
    ),
    "phone_events/health": (
        "phone.health_observed",
        {
            "metrics": phone_events.METRICS,
            "sleep_stages": phone_events.SLEEP_STAGES,
            "stand_states": phone_events.STAND_STATES,
        },
    ),
    "phone_events/measure": ("phone.measure_observed", {"metrics": phone_events.MEASURES}),
    "phone_events/state": (
        "phone.state_observed",
        {"signals": (*phone_events.SIGNAL_VALUES, phone_events.BATTERY_SIGNAL)},
    ),
    "phone_events/track": ("phone.track_observed", {"columns": tuple(FIX_COLUMNS)}),
}

# where in a valid case a listed word must appear: (the file's list, the payload field it fills)
LISTED_IN: dict[str, str] = {
    "activities": "activity", "confidences": "confidence", "metrics": "metric",
    "sleep_stages": "detail", "stand_states": "detail", "signals": "signal",
}


class Brain:
    """The phone route over a real log for one paired device, at the world's clock."""

    def __init__(self, tmp_path: Path) -> None:
        """Pair the phone and build the app over a fresh log under ``tmp_path``."""
        self.token = pair_device(tmp_path, "phone")
        self.client: TestClient
        self.log: Path
        self.client, _hub, self.log = brain_client(tmp_path, speaks=False)

    def post(self, frames: list[Any]) -> dict[str, Any]:
        """The answer to one batch."""
        reply = self.client.post(
            PHONE_EVENTS_PATH, json={"events": frames}, headers=bearer(self.token),
        )
        assert reply.status_code == 200, reply.text
        body: dict[str, Any] = reply.json()
        return body

    def acks(self, frames: list[Any]) -> list[dict[str, Any]]:
        """The acks of one batch."""
        acks: list[dict[str, Any]] = self.post(frames)["acks"]
        return acks

    def stored(self) -> list[dict[str, Any]]:
        """The payloads of the phone's rows, in the order they were written."""
        with closing(sqlite3.connect(self.log)) as conn:
            return [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM events WHERE type LIKE 'phone.%' ORDER BY id",
                )
            ]


@pytest.fixture
def brain(tmp_path: Path) -> Brain:
    """A brain with one paired phone."""
    return Brain(tmp_path)


def _frames(event_type: str, cases: list[Case], first: int) -> list[dict[str, Any]]:
    return [
        {
            "event_uid": fixed_uid(first + index), "event_type": event_type,
            "ts_epoch_ms": WORLD_NOW_MS, "payload": case.value,
        }
        for index, case in enumerate(cases)
    ]


@pytest.mark.parametrize("name", list(TYPES))
def test_the_brain_takes_every_valid_payload_of_a_type_and_refuses_every_invalid_one(
    brain: Brain, contracts: Contracts, name: str,
) -> None:
    """Valid cases are acknowledged and stored whole; invalid ones are refused and leave no row."""
    event_type, lists = TYPES[name]
    inputs = contracts.inputs(name, route=ROUTE)
    assert inputs.doc["event_type"] == event_type

    with brain_at(inputs.now_ms):
        taken = brain.acks(_frames(event_type, inputs.valid, 1))
        refused = brain.acks(_frames(event_type, inputs.invalid, 1_000))

    assert [c.name for c, ack in zip(inputs.valid, taken, strict=True) if not ack["ok"]] == []
    assert brain.stored() == [case.value for case in inputs.valid]
    assert {
        case.name: (ack["ok"], ack.get("code"))
        for case, ack in zip(inputs.invalid, refused, strict=True)
    } == {case.name: (False, case.refusal) for case in inputs.invalid}
    for key, listed in lists.items():
        held = tuple(inputs.doc[key])
        # the columns of a fix are positions; the other lists are sets of words
        assert (held == listed) if key == "columns" else (set(held) == set(listed)), (
            f"{key}: the brain's list changed to {listed}"
        )
        if key in LISTED_IN:
            used = {case.value.get(LISTED_IN[key]) for case in inputs.valid}
            assert set(listed) <= used, f"{key}: no valid case for {sorted(set(listed) - used)}"


def test_the_brain_takes_the_frame_a_phone_sends_and_refuses_one_with_more_in_it(
    brain: Brain, contracts: Contracts,
) -> None:
    """Only the four keys of an event frame; another type than the phone's is not allowed."""
    inputs = contracts.inputs("phone_events/frame", route=ROUTE)
    with brain_at(inputs.now_ms):
        taken = brain.acks([case.value for case in inputs.valid])
        refused = brain.acks([case.value for case in inputs.invalid])
    assert [c.name for c, ack in zip(inputs.valid, taken, strict=True) if not ack["ok"]] == []
    assert len(brain.stored()) == len(inputs.valid)
    assert {
        case.name: (ack["ok"], ack["code"])
        for case, ack in zip(inputs.invalid, refused, strict=True)
    } == {case.name: (False, case.refusal) for case in inputs.invalid}


def test_the_ack_the_phone_reads_for_each_answer_the_brain_gives(
    brain: Brain, contracts: Contracts,
) -> None:
    """One frame in, one ack out: done, or refused for good with a code, or ``retry`` to resend."""
    location = {"lat": 48.4284, "lng": -123.3656, "accuracy_m": 120}
    with sqlite3.connect(brain.log) as conn:
        conn.execute(
            "CREATE TRIGGER fail_one BEFORE INSERT ON events "
            f"WHEN NEW.event_uid = '{fixed_uid(5)}' BEGIN SELECT RAISE(ABORT, 'disk is full'); END",
        )
    frames = {
        "ok": ("phone.location_observed", location),
        "bad_event": ("phone.location_observed", {}),
        "event_type_not_allowed": ("repo.state_observed", {"repo_path": "/r"}),
        "event_too_large": ("phone.location_observed", {"x": "a" * MAX_EVENT_CHARS}),
        "retry": ("phone.location_observed", location),
    }
    with brain_at():
        answers = {
            variant: brain.post([{
                "event_uid": fixed_uid(index), "event_type": kind,
                "ts_epoch_ms": WORLD_NOW_MS, "payload": payload,
            }])
            for index, (variant, (kind, payload)) in enumerate(frames.items(), start=1)
        }
    contracts.output(
        "phone_events/ack",
        examples=answers,
        exact=("type", "ok", "code"), now_ms=WORLD_NOW_MS, route=ROUTE,
        description=(
            "The answer to a batch: one ack per frame, in order, with the frame's `event_uid` "
            "(null when the frame had none). `ok` is true when the event is stored or was "
            "already; false carries a `code`: `bad_event`, `event_type_not_allowed` and "
            "`event_too_large` are refused for good, `retry` means the brain could not write it "
            "and the phone keeps the frame and sends it again."
        ),
    )
    assert [body["acks"][0].get("code") for body in answers.values()] == [
        None, "bad_event", "event_type_not_allowed", "event_too_large", "retry",
    ]
