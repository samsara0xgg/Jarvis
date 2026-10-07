"""ADR 0174: Allen sets her voice volume and speed by saying so, through the model's one tool.

The tool turns the model's direction words into program-owned steps inside fixed bounds;
the holder keeps current and default; her MiniMax task start
reads the holder each time, also when a spare session was connected before the tool ran.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from jarvis.execution.tools import ToolError, build_default_registry
from jarvis.execution.voice_tools import build_voice_tool
from jarvis.shared import CallerPrincipal
from jarvis.state.voice_settings import VoiceSettings
from jarvis.surface import voice_tts
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_wave2_streaming_media import _FakeWebSocket

if TYPE_CHECKING:
    from pathlib import Path


def _call(voice: VoiceSettings, **args: Any) -> dict[str, Any]:  # noqa: ANN401 - the model's args
    (tool,) = build_voice_tool(voice)
    return dict(tool.handler(args, None))  # type: ignore[arg-type]


def test_the_tool_is_on_the_models_menu_up_front_and_only_the_models() -> None:
    """set_voice is in the first request's catalog, for the model only, and only when wired."""
    registry = build_default_registry(voice_settings=VoiceSettings())
    (definition,) = (d for d in registry.get_definitions() if d.name == "set_voice")
    assert definition.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert not definition.deferred
    assert "set_voice" not in {d.name for d in build_default_registry().get_definitions()}


@pytest.mark.parametrize(
    ("word", "volume"),
    [("louder_a_bit", 420), ("louder_a_lot", 500), ("quieter_a_bit", 210), ("quieter_a_lot", 150)],
)
def test_a_volume_word_is_one_program_step(word: str, volume: int) -> None:
    """The model names a direction and size; the program owns the number."""
    voice = VoiceSettings()
    result = _call(voice, volume=word)
    assert (result["volume_percent"], result["speed"]) == (volume, 1.0)
    assert result["changed"] == ["volume"]
    assert voice.snapshot() == (volume, 1.0)


@pytest.mark.parametrize(
    ("word", "speed"),
    [("faster_a_bit", 1.2), ("faster_a_lot", 1.4), ("slower_a_bit", 0.8), ("slower_a_lot", 0.6)],
)
def test_a_speed_word_is_one_program_step(word: str, speed: float) -> None:
    """Speed steps are 0.2 and 0.4."""
    voice = VoiceSettings()
    result = _call(voice, speed=word)
    assert (result["volume_percent"], result["speed"]) == (300, speed)
    assert result["changed"] == ["speed"]


def test_bounds_clamp_and_say_at_the_limit() -> None:
    """Volume stops at 30%, speed at 1.8 and 0.6; the result says it is at the limit."""
    voice = VoiceSettings()
    for _ in range(5):
        result = _call(voice, volume="quieter_a_lot")
    assert result["volume_percent"] == 30
    assert result["at_limit"] is True
    assert result["changed"] == []
    for _ in range(4):
        result = _call(voice, speed="faster_a_lot")
    assert result["speed"] == 1.8
    assert result["at_limit"] is True
    assert _call(voice, speed="slower_a_bit")["at_limit"] is False


def test_the_ceiling_is_500() -> None:
    """500% is the most: above it MiniMax's audio starts to clip (measured 2026-10-07)."""
    voice = VoiceSettings()
    voice.set_current(290, 1.0)
    result = _call(voice, volume="louder_a_lot")
    assert result["volume_percent"] == 500
    assert result["at_limit"] is True


def test_an_unknown_word_is_the_models_error() -> None:
    """A raw number or any other word is rejected as an invalid argument."""
    with pytest.raises(ToolError):
        _call(VoiceSettings(), volume="120")


def test_remember_keeps_the_default_across_a_restart_and_reset_forgets_it(tmp_path: Path) -> None:
    """Only the default is saved; reset puts the factory voice back and forgets it."""
    path = tmp_path / "voice-settings.json"
    voice = VoiceSettings(path)
    _call(voice, volume="louder_a_bit", speed="slower_a_bit", remember=True)
    assert _call(voice, volume="louder_a_bit")["remembered"] is False  # 500, not kept
    assert VoiceSettings(path).snapshot() == (420, 0.8)
    reset = _call(voice, reset=True)
    assert (reset["volume_percent"], reset["speed"], reset["reset"]) == (300, 1.0, True)
    assert VoiceSettings(path).snapshot() == (300, 1.0)


def test_a_missing_or_broken_file_is_the_factory_voice(tmp_path: Path) -> None:
    """A missing, broken or out-of-range file never breaks the boot."""
    path = tmp_path / "voice-settings.json"
    assert VoiceSettings(path).snapshot() == (300, 1.0)
    path.write_text("{not json", encoding="utf-8")
    assert VoiceSettings(path).snapshot() == (300, 1.0)
    path.write_text('{"percent": 9000, "speed": 0.1}', encoding="utf-8")
    assert VoiceSettings(path).snapshot() == (500, 0.6)


def test_the_end_of_a_conversation_returns_current_to_the_default(tmp_path: Path) -> None:
    """Conversation going on to off puts the current voice back to the default, once."""
    voice = VoiceSettings(tmp_path / "voice-settings.json")
    _call(voice, volume="louder_a_bit", remember=True)
    _call(voice, volume="quieter_a_lot", speed="faster_a_bit")
    controls = VoiceControls()
    controls.on_conversation_end = voice.end_conversation
    controls.update(conversation=True)
    controls.update(conversation=True)
    assert voice.snapshot() == (210, 1.2)
    controls.update(conversation=False)
    assert voice.snapshot() == (420, 1.0)
    _call(voice, speed="faster_a_bit")
    controls.update(conversation=False)  # already off: no end, nothing returns
    assert voice.snapshot() == (420, 1.2)


def _client(voice: VoiceSettings | None, volume: int = 1) -> voice_tts.MiniMaxWSClient:
    return voice_tts.MiniMaxWSClient(api_key="key", volume=volume, voice_settings=voice)


def _session(voice: VoiceSettings | None, volume: int = 1) -> voice_tts.TTSSession:
    return _client(voice, volume).create_tts_session(
        endpoint_index=0, language="en", idle_close_s=1.0,
        command_queue_capacity=1, audio_queue_capacity=1,
    )


def test_the_session_task_start_carries_the_current_voice_and_a_stale_spare_reconnects() -> None:
    """A spare connected before the tool ran is not used for the answer that follows it."""
    async def _body() -> list[dict[str, Any]]:
        sockets: list[_FakeWebSocket] = []

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            del additional_headers
            sockets.append(_FakeWebSocket())
            return sockets[-1]

        voice = VoiceSettings()
        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = _session(voice, volume=2)
            await session.connect()  # the spare: connected while he was still talking
            voice.set_current(150, 1.2)  # "louder" ran its tool
            await session.open("R", 1)  # her answer takes the spare
            await session.close()
        return [s.sent[0] for s in sockets]

    first, second = asyncio.run(_body())
    assert first["voice_setting"]["vol"] == 6.0
    assert first["voice_setting"]["speed"] == 1.0
    assert second["voice_setting"]["vol"] == 3.0
    assert second["voice_setting"]["speed"] == 1.2


def test_an_unchanged_spare_is_kept() -> None:
    """With no voice change the spare binds without a second handshake."""
    async def _body() -> int:
        count = 0

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            nonlocal count
            del additional_headers
            count += 1
            return _FakeWebSocket()

        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            session = _session(VoiceSettings())
            await session.connect()
            await session.open("R", 1)
            await session.close()
        return count

    assert asyncio.run(_body()) == 1


def test_the_one_shot_task_start_reads_the_holder_too() -> None:
    """The one-shot client reads the holder at its task start."""
    async def _body() -> dict[str, Any]:
        ws = _FakeWebSocket()
        ws.audio_gate.set()

        async def _connect(_url: str, *, additional_headers: dict[str, str]) -> _FakeWebSocket:
            del additional_headers
            return ws

        voice = VoiceSettings()
        voice.set_current(50, 0.8)
        with patch.object(voice_tts, "_ws_connect", side_effect=_connect):
            await _client(voice).synthesize("Hello.")
        return ws.sent[0]

    setting = asyncio.run(_body())["voice_setting"]
    assert (setting["vol"], setting["speed"]) == (0.5, 0.8)
