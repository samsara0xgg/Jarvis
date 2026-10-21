"""ADR 0041: wave mode listens without a wake word and stops Jarvis when Allen talks.

The duplex session runs on the scripted ingress of the Wave 3 tests with a wake
engine that never fires, so every commit here is one no wake hit armed. The
controls checks drive the real ``/inherent/controls`` and ``/inherent/ws``.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server.app import create_app
from jarvis.surface.inherent_server.deps import InherentDeps
from jarvis.surface.voice_controls import VoiceControls
from tests.support.conversation_mode import ModeSession
from tests.support.voice_input import wait_until_true


def test_wave_mode_commits_speech_with_no_wake_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sentences, no wake word: both reach ASR as their own utterances."""
    rig = ModeSession(monkeypatch)
    rig.speak()
    wait_until_true(lambda: len(rig.pipeline.calls) == 1)
    rig.speak()
    wait_until_true(lambda: len(rig.pipeline.calls) == 2)
    assert rig.session.metrics().wake_detections == 0
    assert rig.pipeline.calls[0]["utterance_id"] != rig.pipeline.calls[1]["utterance_id"]
    # No wake word opened them, so a "Hey Jarvis" in them is a greeting, kept.
    assert [call["wake_lead"] for call in rig.pipeline.calls] == [False, False]
    assert not rig.stopped.is_set()
    rig.close()


def test_speech_over_jarvis_stops_it_and_is_still_heard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allen talks while Jarvis speaks: the stop fires at onset, the words commit."""
    rig = ModeSession(monkeypatch, speaking=True)
    rig.speak()
    wait_until_true(rig.stopped.is_set)
    wait_until_true(lambda: len(rig.pipeline.calls) == 1)
    rig.close()


@pytest.mark.parametrize("switch", ["conversation", "muted"])
def test_leaving_wave_mode_or_muting_needs_the_wake_word_again(
    monkeypatch: pytest.MonkeyPatch, switch: str,
) -> None:
    """An arm that heard nothing yet is dropped, so later speech commits nothing."""
    rig = ModeSession(monkeypatch)
    epoch = rig.ingress.stream_epoch
    assert epoch is not None
    for _ in range(4):  # idle frames: the conversation arm is in place
        rig.backend.emit(epoch=epoch, value=0)
        time.sleep(0.002)
    if switch == "conversation":
        rig.conversation = False
    else:
        rig.muted = True
    rig.speak()
    time.sleep(0.2)
    assert rig.pipeline.calls == []
    # Positive control: back in wave mode, the same speech commits.
    rig.conversation, rig.muted = True, False
    rig.speak()
    wait_until_true(lambda: len(rig.pipeline.calls) == 1)
    rig.close()


def test_the_surface_sets_the_switch_and_its_last_disconnect_clears_it() -> None:
    """Resonance drives the switch; a daemon with no surface left stops listening."""
    controls = VoiceControls()
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: None,
            broadcaster=InherentBroadcaster(),
            controls=controls,
        ),
    )
    with TestClient(app) as client:
        with client.websocket_connect("/inherent/ws"):
            state = client.post("/inherent/controls", json={"conversation": True}).json()
            assert state["conversation"] is True
            assert client.post("/inherent/controls", json={}).json()["conversation"] is True
        wait_until_true(lambda: not controls.conversation)
        assert client.post("/inherent/controls", json={}).json()["conversation"] is False


def test_the_wake_word_opens_conversation_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0102: a wake hit flips the switch as a tap would; later speech needs no wake."""
    rig = ModeSession(monkeypatch, conversation=False, wake=True)
    rig.idle(0.1)
    wait_until_true(lambda: rig.changes == [(True, "wake")])
    rig.wake_engine._detections = set()  # noqa: SLF001 - no more wake hits from here
    rig.speak()
    wait_until_true(lambda: len(rig.pipeline.calls) >= 1)
    rig.close()
    assert rig.changes == [(True, "wake")]


def test_quiet_ends_conversation_mode_but_her_speech_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0102: nobody talking and Jarvis silent ends it; while she talks it stays."""
    rig = ModeSession(monkeypatch, speaking=True, idle_exit_s=0.05)
    rig.idle(0.2)
    assert rig.changes == []
    rig.speaking = False
    rig.idle(0.2)
    rig.close()
    assert rig.changes == [(False, "idle")]


def test_a_turn_still_working_keeps_conversation_mode_open_past_the_quiet_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0102: a turn working past the window ends nothing; quiet after it does."""
    working = [True]
    calls = [0]

    def turn_working() -> bool:
        calls[0] += 1
        return working[0]

    rig = ModeSession(monkeypatch, idle_exit_s=0.05, turn_working=turn_working)
    try:
        rig.idle(0.2)
        assert rig.changes == []
        assert 1 <= calls[0] <= 6  # asked when the window runs out, not on every frame
        working[0] = False
        rig.idle(0.2)
        assert rig.changes == [(False, "idle")]
    finally:
        rig.close()
