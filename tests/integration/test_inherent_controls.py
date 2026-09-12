"""``POST /inherent/controls`` — the mute switches' wire (ADR-0015 D2).

Asserts on the HTTP payload the desktop surface reads: ``{}`` is a pure
read, a partial body flips only the named switch, every answer is the full
state, and the route does not exist when no controls are injected. The
speech switch is then followed to its observable, the PCM the player hands
the audio device: ones unmuted, a 10 ms ramp to exact zeros muted, ones again.
"""

from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.voice_controls import VoiceControls
from jarvis.surface.voice_tts import AudioStreamPlayer


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
    assert controls.speech_muted
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


def test_speech_mute_over_the_wire_zeroes_the_player_output_and_unmute_restores_it() -> None:
    """Mute is the player's gain as the runtime binds it: the device gets zeros, then ones again."""
    player = AudioStreamPlayer(sample_rate_hz=16_000, lazy_open=True)
    controls = VoiceControls(
        on_speech_muted=lambda muted: player.set_gain(0.0 if muted else 1.0, 10.0),
    )
    client = TestClient(
        create_app(
            InherentDeps(
                submit_callable=_noop_submit,
                broadcaster=InherentBroadcaster(),
                controls=controls,
            )
        )
    )
    block = np.empty((256, 1), dtype=np.float32)

    def play_block() -> np.ndarray:
        player.write(np.ones(256, dtype=np.float32).tobytes())
        player._callback(block, 256, None, None)  # noqa: SLF001 - PortAudio's entry, driven by hand
        return block[:, 0].copy()

    assert np.all(play_block() == 1.0)
    client.post("/inherent/controls", json={"speech_muted": True})
    ramp = play_block()  # 10 ms at 16 kHz: 160 samples down to exactly 0.0, then silence
    assert ramp[0] == 1.0
    assert 0.0 < ramp[80] < 1.0
    assert np.all(ramp[160:] == 0.0)
    assert np.all(play_block() == 0.0)
    client.post("/inherent/controls", json={"speech_muted": False})
    play_block()  # the ramp back up
    assert np.all(play_block() == 1.0)
