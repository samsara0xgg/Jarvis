"""ADR 0052 — the Settings page's routes, the file they keep, and the voice volume at the device.

Each check asserts what ``/inherent/settings`` serves (what the page shows),
what lands in ``<runtime root>/settings.json`` and what the next boot's view
reads from it, or the samples the player hands its output callback.
"""

from __future__ import annotations

import asyncio
import functools
import json
from typing import TYPE_CHECKING, Any

import numpy as np
from fastapi.testclient import TestClient

from jarvis.runtime.settings import Settings, apply_settings
from jarvis.surface import voice_tts
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

# The keys the page reads, as config/jarvis.yaml held them on 2026-09-25.
YAML: dict[str, Any] = {
    "llm": {
        "default_preset": "luna",
        "presets": {
            "luna": {"model": "gpt-5.6-luna"},
            "gpt6-luna": {"model": "gpt-6-luna"},
            "gpt6-sol": {"model": "gpt-6-sol"},
        },
    },
    "work_state": {"preset": "gpt6-luna"},
    "daily_report": {"preset": "gpt6-sol"},
    "observer": {
        "repos": ["~/Projects/jarvis", "~/Projects/typlus"], "timesink": {"enabled": True},
    },
    "memory": {"retain_audio": True, "audio_retention_days": 30},
    "tools": {"screen": {"retention_days": 7}},
    "realtime": {
        "wake_threshold": 0.95,
        "tts_voice": "Chinese (Mandarin)_Warm_Bestie",
        "output_device": None,
        "gpt_live": {"enabled": True},
        "single_audio_ingress": {"echo_cancellation": "auto"},
    },
}
DEVICES = {
    "input": ["reSpeaker XVF3800 4-Mic Array", "MacBook Pro Microphone"],
    "output": ["MacBook Pro Speakers", "Multi-Output Device 2"],
}

# What CoreAudio says "System default" is; a kind it cannot answer for is absent.
DEFAULT_DEVICES = {"input": "MacBook Pro Microphone", "output": "MacBook Pro Speakers"}


def _client(root: Path) -> TestClient:
    """The routes wired the way the daemon wires them, over a boot of ``YAML`` plus the file."""
    settings = Settings(
        root, apply_settings(YAML, root), DEVICES.__getitem__, DEFAULT_DEVICES.get,
    )

    async def save(changes: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(settings.update, changes)

    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        settings_read=functools.partial(asyncio.to_thread, settings.read),
        settings_update=save,
    )))


def test_the_page_reads_what_jarvis_booted_with(tmp_path: Path) -> None:
    """GET: current values in the page's words, the choices, nothing waiting for a restart."""
    body = _client(tmp_path).get("/inherent/settings").json()
    assert body["values"] == {
        "reply_language": "follow", "wake_threshold": 0.95, "tts_voice": "暖心闺蜜",
        "tts_volume": 1.0, "output_device": "System default", "input_device": "System default",
        "gpt_live": True, "timesink": True, "keep_audio": True,
        "audio_days": 30, "screenshot_days": 7,
        "repos": ["~/Projects/jarvis", "~/Projects/typlus"],
        "model_conversation": "gpt-5.6-luna", "model_background": "gpt-6-luna",
        "model_report": "gpt-6-sol",
    }
    assert body["options"]["input_device"] == ["System default", *DEVICES["input"]]
    assert body["options"]["output_device"] == ["System default", *DEVICES["output"]]
    assert len(body["options"]["tts_voice"]) == 36  # 32 Mandarin + the 4 English setup voices
    # A voice first-run setup offers carries setup's name; the rest their MiniMax name.
    assert "舒缓女声" in body["options"]["tts_voice"]
    assert "Warm Hearted Girl" in body["options"]["tts_voice"]
    assert body["options"]["audio_days"] == [7, 30, 90, None]  # None: keep forever (ADR 0067)
    assert body["defaults"] == {
        "input_device": "MacBook Pro Microphone", "output_device": "MacBook Pro Speakers",
    }
    assert body["restart_pending"] is False


def test_saving_waits_for_the_next_boot(tmp_path: Path) -> None:
    """POST writes the file; the running view says restart, the next boot reads the new values."""
    changes = {
        "input_device": "MacBook Pro Microphone", "tts_voice": "Crisp Girl",
        "wake_threshold": 0.9, "reply_language": "en",
        "audio_days": None, "screenshot_days": 90,
    }
    body = _client(tmp_path).post("/inherent/settings", json={"changes": changes}).json()
    assert body["restart_pending"] is True
    assert {key: body["values"][key] for key in changes} == changes
    assert json.loads((tmp_path / "settings.json").read_text()) == {
        "input_device": "MacBook Pro Microphone", "tts_voice": "Chinese (Mandarin)_Crisp_Girl",
        "wake_threshold": 0.9, "reply_language": "en",
        "audio_days": None, "screenshot_days": 90,
    }
    booted = apply_settings(YAML, tmp_path)
    assert booted["memory"]["audio_retention_days"] is None
    assert booted["tools"]["screen"]["retention_days"] == 90
    assert booted["realtime"]["input_device"] == "MacBook Pro Microphone"
    assert booted["reply_language"] == "en"
    after_restart = _client(tmp_path).get("/inherent/settings").json()
    assert after_restart["restart_pending"] is False
    assert after_restart["values"]["tts_voice"] == "Crisp Girl"
    # Choosing the booted value again means nothing waits for a restart.
    again = _client(tmp_path).post(
        "/inherent/settings", json={"changes": {"input_device": "MacBook Pro Microphone"}}
    )
    assert again.json()["restart_pending"] is False


def test_a_value_the_page_cannot_hold_is_refused(tmp_path: Path) -> None:
    """400 for an unknown key, an out-of-range number, a device that is not plugged in."""
    client = _client(tmp_path)
    for changes in (
        {"model_report": "gpt-6-luna"},
        {"wake_threshold": 1.5},
        {"tts_volume": 2},
        {"gpt_live": "yes"},
        {"input_device": "USB Mic that left"},
        {"tts_voice": "Nobody"},
        {"audio_days": 45},
        {"screenshot_days": 7.0},
    ):
        assert client.post("/inherent/settings", json={"changes": changes}).status_code == 400
    assert not (tmp_path / "settings.json").exists()


def test_an_old_saved_mac_aec_is_ignored(tmp_path: Path) -> None:
    """Echo cancellation follows the microphone now: the page has no such key, a saved one is inert."""
    (tmp_path / "settings.json").write_text(json.dumps({"mac_aec": True, "timesink": False}))
    booted = apply_settings(YAML, tmp_path)
    assert booted["realtime"]["single_audio_ingress"]["echo_cancellation"] == "auto"
    assert booted["observer"]["timesink"]["enabled"] is False
    body = _client(tmp_path).get("/inherent/settings").json()
    assert "mac_aec" not in body["values"]
    refused = _client(tmp_path).post("/inherent/settings", json={"changes": {"mac_aec": True}})
    assert refused.status_code == 400


def test_a_broken_file_boots_on_the_yaml(tmp_path: Path) -> None:
    """A hand-edited file with a bad value or bad JSON never changes the boot."""
    (tmp_path / "settings.json").write_text(json.dumps({"wake_threshold": 7, "timesink": False}))
    booted = apply_settings(YAML, tmp_path)
    assert booted["realtime"]["wake_threshold"] == 0.95
    assert booted["observer"]["timesink"]["enabled"] is False
    (tmp_path / "settings.json").write_text("{not json")
    assert apply_settings(YAML, tmp_path) == YAML


def test_voice_volume_scales_what_the_device_plays() -> None:
    """The player at volume 0.5 hands the device (and the echo canceller's tap) half the level."""
    heard: list[np.ndarray] = []
    player = voice_tts.AudioStreamPlayer(
        sample_rate_hz=48000, lazy_open=True, volume=0.5,
        playback_tap=lambda block, _rate: heard.append(block.copy()),
    )
    player.write(np.full(1024, 0.8, dtype=np.float32).tobytes())
    out = np.zeros((1024, 1), dtype=np.float32)
    player._tapped_callback(out, 1024, None, None)  # noqa: SLF001 — PortAudio's entry point.
    assert np.allclose(out[:, 0], 0.4)
    assert np.allclose(heard[0], 0.4)
