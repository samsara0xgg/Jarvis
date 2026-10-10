"""ADR 0219: a phone's spoken ``say`` that another device heard first is no turn.

Acceptance checks against the real ``/phone/ws`` route over a real event log, as
``test_phone_judge`` has it; ``heard_elsewhere`` is a fake that records what it was asked, and
the verdict of a flagged say still comes from the real shared judge.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.surface.terminal_events import phone_turn_id
from tests.integration.test_phone_judge import TURN_WORDS, _open, _Rig, _say

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    """A rig over an empty directory; a test builds it with the hooks it needs."""
    return _Rig(tmp_path)


# (the flags of the spoken say)
FLAGS = {
    "unflagged": {},
    "over her": {"over_her": True},
    "in conversation mode": {"conversation": True},
    "both flags": {"over_her": True, "conversation": True},
}


@pytest.mark.parametrize("name", list(FLAGS))
def test_a_spoken_say_another_device_heard_first_is_answered_elsewhere_and_writes_nothing(
    rig: _Rig, name: str,
) -> None:
    """Flagged or not: ``verdict: elsewhere``, ``turn_id: null``, no row, no barge, no Jev."""
    rig.build(elsewhere=lambda *_a: True)
    with rig.connect() as ws:
        _open(ws)
        reply = _say(ws, "u1", TURN_WORDS, **FLAGS[name])
    assert reply == {
        "type": "said", "utterance_id": "u1", "turn_id": None, "verdict": "elsewhere",
    }
    assert rig.elsewhere_asked == [("iphone", phone_turn_id("iphone", "u1"), TURN_WORDS)]
    assert rig.turns() == []
    assert rig.calls == []  # no barge, no cancel: she is not being talked to
    assert rig.asked == []  # and the judge spent no Jev request


def test_typed_says_are_never_checked(rig: _Rig) -> None:
    """Typing on the phone is a turn whatever another device heard, flagged or not."""
    rig.build(elsewhere=lambda *_a: True)
    with rig.connect() as ws:
        _open(ws)
        plain = _say(ws, "t1", TURN_WORDS, spoken=False)
        flagged = _say(ws, "t2", TURN_WORDS, spoken=False, over_her=True, conversation=True)
    assert [(r["verdict"], r["turn_id"]) for r in (plain, flagged)] == [
        ("turn", phone_turn_id("iphone", "t1")), ("turn", phone_turn_id("iphone", "t2")),
    ]
    assert rig.elsewhere_asked == []
    assert rig.turns() == [plain["turn_id"], flagged["turn_id"]]


@pytest.mark.parametrize("name", list(FLAGS))
def test_a_say_nobody_else_heard_is_judged_and_recorded_as_before(rig: _Rig, name: str) -> None:
    """The check answering no changes nothing: the words are a turn."""
    rig.build(elsewhere=lambda *_a: False)
    with rig.connect() as ws:
        _open(ws)
        reply = _say(ws, "u1", TURN_WORDS, **FLAGS[name])
    assert (reply["verdict"], reply["turn_id"]) == ("turn", phone_turn_id("iphone", "u1"))
    assert len(rig.elsewhere_asked) == 1
    assert rig.turns() == [reply["turn_id"]]


@pytest.mark.parametrize("name", ["unflagged", "over her"])
def test_a_check_that_fails_leaves_the_words_a_turn(rig: _Rig, name: str) -> None:
    """Another device's log cannot lose the phone's words."""
    def boom(*_args: Any) -> bool:  # noqa: ANN401
        raise RuntimeError

    rig.build(elsewhere=boom)
    with rig.connect() as ws:
        _open(ws)
        reply = _say(ws, "u1", TURN_WORDS, **FLAGS[name])
    assert (reply["verdict"], reply["turn_id"]) == ("turn", phone_turn_id("iphone", "u1"))
    assert rig.turns() == [reply["turn_id"]]


def test_a_host_without_the_check_records_an_unflagged_say_at_once(rig: _Rig) -> None:
    """No field, no check: the say is a turn as it always was."""
    rig.build()
    with rig.connect() as ws:
        _open(ws)
        reply = _say(ws, "u1", TURN_WORDS)
    assert (reply["verdict"], reply["turn_id"]) == ("turn", phone_turn_id("iphone", "u1"))
    assert rig.elsewhere_asked == []


def test_a_resent_say_keeps_the_turn_it_already_made(rig: _Rig) -> None:
    """The first say was recorded; the Mac heard it a moment later; the resend is the same turn."""
    heard_by_the_mac = threading.Event()
    rig.build(elsewhere=lambda *_a: heard_by_the_mac.is_set())
    with rig.connect() as ws:
        _open(ws)
        first = _say(ws, "u1", TURN_WORDS)
        heard_by_the_mac.set()
        resent = _say(ws, "u1", TURN_WORDS)
        other = _say(ws, "u2", TURN_WORDS)
    assert first == resent
    assert first["turn_id"] == phone_turn_id("iphone", "u1")
    assert (other["verdict"], other["turn_id"]) == ("elsewhere", None)
    assert rig.turns() == [first["turn_id"]]


def test_the_receive_loop_keeps_reading_while_the_log_is_asked(rig: _Rig) -> None:
    """An unflagged say's check runs on a worker thread: a ping is answered meanwhile."""
    release = threading.Event()
    asked = threading.Event()

    def slow(*_args: Any) -> bool:  # noqa: ANN401
        asked.set()
        assert release.wait(10)
        return True

    rig.build(elsewhere=slow)
    with rig.connect() as ws:
        _open(ws)
        ws.send_json({"type": "say", "utterance_id": "c1", "text": TURN_WORDS, "spoken": True})
        assert asked.wait(10)
        ws.send_json({"type": "ping", "t_ns": 7})
        assert ws.receive_json()["type"] == "pong"
        release.set()
        reply = ws.receive_json()
    assert (reply["type"], reply["verdict"], reply["turn_id"]) == ("said", "elsewhere", None)
