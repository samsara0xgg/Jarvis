"""``POST /inherent/controls`` — the mute switches' wire (ADR-0015 D2).

Asserts on the HTTP payload the desktop surface reads: ``{}`` is a pure
read, a partial body flips only the named switch, every answer is the full
state, and the route does not exist when no controls are injected.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls


def _noop_submit(text: str) -> None:
    _ = text


def test_controls_wire_reads_and_flips_each_switch_independently() -> None:
    """``{}`` reads; a partial body flips one switch; every answer is the full state."""
    controls = VoiceControls()
    client = TestClient(
        create_app(
            InherentDeps(
                submit_callable=_noop_submit,
                broadcaster=InherentBroadcaster(),
                controls=controls,
            )
        )
    )

    assert client.post("/inherent/controls", json={}).json() == {
        "mic_muted": False,
        "speech_muted": False,
    }
    assert client.post("/inherent/controls", json={"mic_muted": True}).json() == {
        "mic_muted": True,
        "speech_muted": False,
    }
    assert client.post("/inherent/controls", json={"speech_muted": True}).json() == {
        "mic_muted": True,
        "speech_muted": True,
    }
    # The voice owners read through the bound methods the runtime hands them.
    assert controls.mic_is_muted()
    assert controls.speech_is_muted()
    assert client.post("/inherent/controls", json={"mic_muted": False}).json() == {
        "mic_muted": False,
        "speech_muted": True,
    }
    assert client.post("/inherent/controls", json={"mic_muted": "loud"}).status_code == 422


def test_controls_route_absent_without_injected_controls() -> None:
    """A v1 deployment without injected controls keeps the route table as it was."""
    client = TestClient(
        create_app(InherentDeps(submit_callable=_noop_submit, broadcaster=InherentBroadcaster()))
    )
    assert client.post("/inherent/controls", json={}).status_code == 404
