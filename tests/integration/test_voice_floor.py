"""ADR 0407: one device answers his voice at a time, named by the brain before he speaks.

Shown on the real code, with no model and no network: (a) the floor's rule, as the standing
each device is told after what the devices reported; (b) a phone's socket is told its standing
after ``ready`` and at each change, its touch and its conversation move the floor, and its
spoken words while another device is in a conversation are answered ``elsewhere`` and written
nowhere; (c) a terminal's controls poll is answered with its standing, its conversation mode
takes the floor, and its judged utterance while a phone is in a conversation is ``elsewhere``;
(d) the Mac's capture session drops a wake hit while another device is in a conversation and
leaves its own conversation mode, listens unseen to a wake hit another device answers (shown
only once the brain takes the words), and shows its own at once.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime.daemon.phone_brain import heard_elsewhere_check
from jarvis.surface.terminal_events import phone_turn_id
from jarvis.surface.terminal_listen import ListenHooks
from jarvis.surface.voice_floor import VoiceFloor
from tests.support.conversation_mode import ModeSession
from tests.support.phone import JudgeRig, open_ws, say_words
from tests.support.terminal import wait_for
from tests.support.terminal_listen import (
    ask_op,
    connect_listening,
    listening_client,
    logged_utterances,
    utterance_frame,
)
from tests.support.voice_input import wait_until_true

if TYPE_CHECKING:
    from pathlib import Path

# --- (a) the rule ------------------------------------------------------------------------

_BOTH_HEAR = [
    ("macbook", {"hears": True, "used_ago_s": 5.0}),
    ("ipad", {"hears": True, "used_ago_s": 60.0}),
]

FLOOR_ROWS = [
    pytest.param([], {"macbook": "mine", "ipad": "mine"}, id="nobody-reported-each-answers"),
    pytest.param(_BOTH_HEAR, {"macbook": "mine", "ipad": "other"}, id="the-one-used-last-answers"),
    pytest.param(
        [*_BOTH_HEAR, ("ipad", {"used_ago_s": 0.0})], {"macbook": "other", "ipad": "mine"},
        id="a-touch-moves-it",
    ),
    pytest.param(
        [("macbook", {"hears": True, "used_ago_s": 5.0}),
         ("ipad", {"hears": False, "used_ago_s": 0.0})],
        {"macbook": "mine", "ipad": "other"},
        id="one-that-cannot-hear-the-wake-word-is-not-picked-by-use",
    ),
    pytest.param(
        [*_BOTH_HEAR, ("ipad", {"busy": True}), ("macbook", {"used_ago_s": 0.0})],
        {"macbook": "taken", "ipad": "mine"},
        id="a-conversation-keeps-it-whatever-he-touches-elsewhere",
    ),
    pytest.param(
        [*_BOTH_HEAR, ("ipad", {"busy": True}), ("macbook", {"busy": True})],
        {"macbook": "mine", "ipad": "taken"},
        id="the-conversation-begun-last-has-it",
    ),
    pytest.param(
        [*_BOTH_HEAR, ("ipad", {"busy": True}), ("ipad", {"busy": False})],
        {"macbook": "mine", "ipad": "other"},
        id="once-it-ends-use-decides-again",
    ),
    pytest.param(
        [*_BOTH_HEAR, ("ipad", {"used_ago_s": 0.0}), ("ipad", {"busy": True}), ("ipad", None)],
        {"macbook": "mine", "ipad": "other"},
        id="a-device-that-leaves-gives-it-up",
    ),
]


@pytest.mark.parametrize(("reports", "standings"), FLOOR_ROWS)
def test_the_floor_is_the_conversation_then_the_device_he_used_last(
    reports: list[tuple[str, dict[str, Any] | None]], standings: dict[str, str],
) -> None:
    """Each device's standing after the reports, in order; ``None`` is the device leaving."""
    floor = VoiceFloor(clock=lambda: 1000.0)
    for device, report in reports:
        if report is None:
            floor.left(device)
        else:
            floor.report(device, **report)
    assert {device: floor.standing(device) for device in standings} == standings


# --- (b) a phone's socket -------------------------------------------------------------------


def _next_floor(ws: Any) -> str:  # noqa: ANN401 - a Starlette test websocket
    while (frame := ws.receive_json())["type"] != "floor":
        pass
    return str(frame["standing"])


def test_a_phone_is_told_where_it_stands_and_its_words_go_nowhere_while_another_talks(
    tmp_path: Path,
) -> None:
    """The standing after ready and at each change; elsewhere answered, nothing written."""
    floor = VoiceFloor()
    rig = JudgeRig(tmp_path)
    rig.build(
        hub={
            "floor": floor,
            "heard_elsewhere": heard_elsewhere_check(floor, tmp_path / "events.db"),
        },
    )
    with rig.client, rig.connect() as ws:  # one event loop, so the socket's end runs after it
        open_ws(ws)
        assert _next_floor(ws) == "mine"
        floor.report("macbook", busy=True)  # he began a conversation on the Mac
        assert _next_floor(ws) == "taken"
        said = say_words(ws, "u1")
        assert (said["verdict"], said["turn_id"]) == ("elsewhere", None)
        floor.report("macbook", busy=False, hears=True, used_ago_s=30.0)  # it ended there
        assert _next_floor(ws) == "other"
        ws.send_json({"type": "floor", "busy": False, "hears": True, "touched": True})
        assert _next_floor(ws) == "mine"  # he picked the phone up
        ws.send_json({"type": "floor", "busy": True, "hears": False})
        wait_for(lambda: floor.standing("macbook") == "taken", "the phone's talk took nothing")
        said = say_words(ws, "u2")
        assert said["verdict"] == "turn"
        ws.close()
        wait_for(lambda: floor.standing("macbook") == "mine", "the phone left and kept the floor")
    assert rig.turns() == [phone_turn_id("iphone", "u2")]


# --- (c) a terminal's socket ----------------------------------------------------------------


def test_a_terminal_hears_where_it_stands_on_its_poll_and_its_conversation_takes_the_floor(
    tmp_path: Path,
) -> None:
    """The poll's report and answer; words heard while the iPad talks are elsewhere."""
    state = {"conversation": False, "quiet": "off"}
    client, hub, log = listening_client(tmp_path)
    assert hub.listening is not None
    hub.listening.hooks = ListenHooks(
        controls=lambda: dict(state),
        set_conversation=lambda on, _why: state.update(conversation=on),
        heard_elsewhere=heard_elsewhere_check(hub.floor, log),
    )
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)

        def ask(op: str, args: dict[str, Any]) -> Any:  # noqa: ANN401 - the reply's value
            rid = uuid.uuid4().hex
            ask_op(ws, op, args, rid)
            while (frame := ws.receive_json()).get("id") != rid:
                pass
            assert frame["ok"] is True, frame
            return frame["value"]

        assert ask("controls", {"busy": False, "hears": True, "idle_s": 2.0})["floor"] == "mine"
        hub.floor.report("ipad", busy=True)  # he began talking to the iPad
        assert ask("controls", {"busy": False, "hears": True, "idle_s": 0.0})["floor"] == "taken"
        judged = ask(
            "utterance", utterance_frame("T1", judge={"conversation": True, "over_her": False}),
        )
        assert judged == {"verdict": "elsewhere"}
        assert logged_utterances(log) == []
        assert ask("conversation", {"on": True, "reason": "tap"}) == {}  # he tapped the Mac
        assert (hub.floor.standing("macbook"), hub.floor.standing("ipad")) == ("mine", "taken")
        assert ask("controls", {"busy": True, "hears": True, "idle_s": 0.0})["floor"] == "mine"
    wait_for(lambda: hub.floor.standing("ipad") == "mine", "the Mac left and kept the floor")


# --- (d) the Mac's capture session ------------------------------------------------------------


class _Shown:
    """The surface's view of the session: each voice phase it was told, with its turn."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, str]] = []

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **_payload: object) -> None:
        self.phases.append((phase, turn_id))

    def of(self, turn_id: str) -> list[str]:
        return [phase for phase, turn in self.phases if turn == turn_id]


class _Judged:
    """The terminal's pipeline: the brain's verdict on the words, carried out as it comes."""

    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.calls.append(dict(kwargs))
        kwargs["on_judged"]("把灯打开。", self.verdict)


def _heard(rig: ModeSession, pipeline: _Judged) -> str:
    rig.speak()
    wait_until_true(lambda: bool(pipeline.calls))
    rig.idle(0.05)
    return str(pipeline.calls[0]["turn_id"])


def test_a_wake_hit_while_another_device_talks_with_him_is_dropped_and_its_mode_goes_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taken: conversation mode on here goes off, and no wake hit or speech commits."""
    rig = ModeSession(monkeypatch, conversation=True, wake=True, floor=lambda: "taken")
    try:
        rig.idle(0.05)
        wait_until_true(lambda: rig.changes == [(False, "floor")])
        rig.speak()
        rig.idle(0.3)
        assert rig.pipeline.calls == []
        assert rig.session.metrics().wake_detections > 0  # it heard the wake word, and let it go
        assert rig.changes == [(False, "floor")]
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("verdict", "shown", "changes"),
    [
        pytest.param(
            "turn", ["transcribing"], [(True, "wake")], id="the-other-device-did-not-wake",
        ),
        pytest.param("elsewhere", [], [], id="the-other-device-answered"),
    ],
)
def test_a_wake_hit_another_device_answers_is_heard_unseen_until_the_brain_takes_it(
    monkeypatch: pytest.MonkeyPatch, verdict: str, shown: list[str],
    changes: list[tuple[bool, str]],
) -> None:
    """Other: nothing shown and no conversation mode, unless the brain's verdict is a turn."""
    surface, pipeline = _Shown(), _Judged(verdict)
    rig = ModeSession(
        monkeypatch, conversation=False, wake=True, floor=lambda: "other", pipeline=pipeline,  # type: ignore[arg-type]
        broadcaster=surface, judged_on_send=True,
    )
    try:
        turn = _heard(rig, pipeline)
        assert surface.of(turn) == shown
        assert rig.changes == changes
    finally:
        rig.close()


def test_a_wake_hit_on_the_device_that_answers_shows_at_once_and_opens_its_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mine, the control for the two above: listening shows at the wake hit, before the words."""
    surface, pipeline = _Shown(), _Judged("turn")
    rig = ModeSession(
        monkeypatch, conversation=False, wake=True, floor=lambda: "mine", pipeline=pipeline,  # type: ignore[arg-type]
        broadcaster=surface, judged_on_send=True,
    )
    try:
        turn = _heard(rig, pipeline)
        assert surface.of(turn)[:2] == ["listening", "transcribing"]
        assert rig.changes[0] == (True, "wake")
    finally:
        rig.close()
