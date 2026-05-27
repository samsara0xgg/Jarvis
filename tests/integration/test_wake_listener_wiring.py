"""ADR-0005 §5.1 review fixes: wake listener gets a real frame source + ducking wired."""

# ruff: noqa: SLF001 — module-private wiring is the surface under test here.
from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from jarvis.runtime import inherent_loop
from jarvis.surface import voice_ducking, voice_tts, voice_wake

if TYPE_CHECKING:
    from pathlib import Path


def test_spawn_wake_listener_passes_real_frame_factory(tmp_path: Path) -> None:
    """_spawn_wake_listener must wire a non-default frame_factory (so wake actually hears audio)."""
    captured: dict[str, object] = {}

    real_init = voice_wake.WakeListener.__init__

    def _spy_init(self: voice_wake.WakeListener, **kwargs: object) -> None:
        captured.update(kwargs)
        real_init(self, **kwargs)  # type: ignore[arg-type]

    pipeline = MagicMock()
    broadcaster = MagicMock()
    tts_pipeline = MagicMock()
    tts_pipeline.is_speaking = lambda: False

    # Patch openwakeword + SileroVad + raw input stream so the spawn path runs
    # without real audio / model dependencies.
    fake_stream = MagicMock()
    fake_stream.read.return_value = (b"\x00" * 2560, False)

    with patch.object(voice_wake.WakeListener, "__init__", _spy_init), \
         patch.object(voice_wake.WakeListener, "start"), \
         patch("jarvis.surface.voice_audio.SileroVad") as mock_silero, \
         patch.object(inherent_loop, "_open_wake_input_stream", return_value=fake_stream):
        mock_silero.return_value = MagicMock()
        listener = inherent_loop._spawn_wake_listener(
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=tmp_path / "silero.onnx",
            tts=tts_pipeline,
        )
        assert listener is not None
        assert captured.get("frame_factory") is not None, (
            "_spawn_wake_listener must pass a real frame_factory (not the zero-frame default)"
        )
        # The frame_factory should NOT be voice_wake._zero_frame (that's the test/fallback default).
        assert captured["frame_factory"] is not voice_wake._zero_frame, (
            "frame_factory still set to _zero_frame; wake will never fire"
        )


def test_spawn_wake_listener_forwards_ducker(tmp_path: Path) -> None:
    """_spawn_wake_listener must forward its ``ducker`` arg into WakeListener."""
    captured: dict[str, object] = {}

    real_init = voice_wake.WakeListener.__init__

    def _spy_init(self: voice_wake.WakeListener, **kwargs: object) -> None:
        captured.update(kwargs)
        real_init(self, **kwargs)  # type: ignore[arg-type]

    pipeline = MagicMock()
    broadcaster = MagicMock()
    tts_pipeline = MagicMock()
    tts_pipeline.is_speaking = lambda: False
    shared_ducker = MagicMock(spec=voice_ducking.SystemAudioDucker)

    fake_stream = MagicMock()
    fake_stream.read.return_value = (b"\x00" * 2560, False)

    with patch.object(voice_wake.WakeListener, "__init__", _spy_init), \
         patch.object(voice_wake.WakeListener, "start"), \
         patch("jarvis.surface.voice_audio.SileroVad") as mock_silero, \
         patch.object(inherent_loop, "_open_wake_input_stream", return_value=fake_stream):
        mock_silero.return_value = MagicMock()
        inherent_loop._spawn_wake_listener(
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=tmp_path / "silero.onnx",
            tts=tts_pipeline,
            ducker=shared_ducker,
        )
        assert captured.get("ducker") is shared_ducker, (
            "_spawn_wake_listener must forward `ducker` into WakeListener"
        )


def test_wake_listener_uses_ducker_around_capture() -> None:
    """Wake detection must duck system output around capture (ADR §5.1).

    The listener calls ``voice_ducking.duck()`` before invoking
    ``capture_callable`` and ``restore()`` after, preventing speaker
    bleed from corrupting the captured utterance.
    """
    ducker_calls: list[str] = []

    fake_ducker = MagicMock(spec=voice_ducking.SystemAudioDucker)
    fake_ducker.duck.side_effect = lambda: ducker_calls.append("duck") or True
    fake_ducker.restore.side_effect = lambda: ducker_calls.append("restore")

    fake_engine = MagicMock()
    detection_iter = iter([0.9, 0.0, 0.0, 0.0])
    fake_engine.predict.side_effect = lambda _frame: {
        "hey_jarvis_v0.1": next(detection_iter, 0.0),
    }

    fake_pipeline = MagicMock()
    fake_pipeline.run_turn.return_value = MagicMock(payload={"transcript": "你好"})
    fake_broadcaster = MagicMock()

    capture_log: list[str] = []
    def _capture() -> bytes:
        capture_log.append(f"duck_state={ducker_calls!r}")
        return b"\x10\x00" * 16000

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=fake_broadcaster,
        capture_callable=_capture,
        threshold=0.5,
        ducker=fake_ducker,
    )
    listener.start()
    time.sleep(0.15)
    listener.request_stop()
    listener.join(timeout_s=1.0)

    # duck must have been called before capture, restore after.
    assert "duck" in ducker_calls
    assert "restore" in ducker_calls
    assert ducker_calls.index("duck") < ducker_calls.index("restore")
    # And during capture, the duck was already applied.
    assert any("duck" in s for s in capture_log)


def test_tts_pipeline_does_not_duck_around_speak() -> None:
    """``_speak`` MUST NOT call ``SystemAudioDucker.duck()`` around synth+play.

    Regression guard for the post-ADR-0005 smoke fix that dropped the
    OS-master-volume duck from the TTS path. Rationale (see
    ``voice_tts.TTSPipeline._speak``): the ducker zeroes macOS master
    output volume, which silences the TTS output stream itself for the
    duration of ``player.write()`` (write blocks up to its 10 s timeout
    while the ring drains). The OS-level master-volume duck is for the
    wake-capture path only (mute speakers while the mic is open). A
    PCM-level gain duck inside the player is what legacy used for
    barge-in attenuation.

    This test inverts the original ``test_tts_pipeline_ducks_around_speak``
    bracket assertion — touching the ducker from ``_speak`` is now a bug.
    """
    fake_ducker = MagicMock(spec=voice_ducking.SystemAudioDucker)
    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize = AsyncMock(return_value=b"\x00" * 960)
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0

    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: None,
        ducker=fake_ducker,
    )
    pipeline.begin_turn("T1", gate_mode="sentence")
    pipeline.handle_chunk("T1", "<voice>你好</voice>")
    pipeline.end_turn("T1")

    # Synth must have happened (the smoke fix only dropped the ducker, not the synth).
    provider.synthesize.assert_awaited_once()
    player.write.assert_called_once()
    # And the ducker MUST be untouched.
    fake_ducker.duck.assert_not_called()
    fake_ducker.restore.assert_not_called()
