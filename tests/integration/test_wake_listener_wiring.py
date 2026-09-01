"""ADR-0005 §5.1 review fixes: wake listener gets a real frame source + ducking wired."""

# ruff: noqa: SLF001 — module-private wiring is the surface under test here.
from __future__ import annotations

import asyncio
import threading
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

    def _record_duck() -> bool:
        ducker_calls.append("duck")
        return True

    fake_ducker = MagicMock(spec=voice_ducking.SystemAudioDucker)
    fake_ducker.duck.side_effect = _record_duck
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


def test_wake_does_not_duck_while_tts_waits_for_first_pcm() -> None:
    """Provider I/O is output-active before the player's first queued byte."""
    synthesis_started = threading.Event()
    release_synthesis = threading.Event()

    async def _synthesize(_text: str) -> bytes:
        synthesis_started.set()
        assert release_synthesis.wait(timeout=2.0)
        await asyncio.sleep(0)
        return b"\x00" * 960

    provider = MagicMock(spec=voice_tts.MiniMaxWSClient)
    provider.synthesize = AsyncMock(side_effect=_synthesize)
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    pipeline = voice_tts.TTSPipeline(
        provider=provider,
        player=player,
        fallback=lambda _text: None,
    )
    pipeline.begin_turn("T-race", gate_mode="sentence")

    synthesis_thread = threading.Thread(
        target=pipeline.handle_chunk,
        args=("T-race", "<voice>你好</voice>"),
        daemon=True,
    )
    synthesis_thread.start()
    assert synthesis_started.wait(timeout=1.0)

    ducker = MagicMock(spec=voice_ducking.SystemAudioDucker)
    capture = MagicMock(return_value=b"\x10\x00" * 16_000)
    engine = MagicMock()
    engine.predict.return_value = {"hey_jarvis_v0.1": 0.9}
    listener = voice_wake.WakeListener(
        engine=engine,
        pipeline=MagicMock(),
        broadcaster=MagicMock(),
        capture_callable=capture,
        threshold=0.5,
        is_speaking_callable=pipeline.is_speaking,
        ducker=ducker,
    )

    try:
        assert pipeline.is_speaking(), (
            "legacy is_speaking() must include synth-before-first-PCM activity"
        )
        assert pipeline.is_output_active()
        listener._run_one_iter()
        engine.predict.assert_not_called()
        capture.assert_not_called()
        ducker.duck.assert_not_called()
    finally:
        release_synthesis.set()
        synthesis_thread.join(timeout=2.0)

    assert not synthesis_thread.is_alive()
    player.write.assert_called_once()


def test_serve_inherent_shutdown_joins_wake_thread_before_stream_close(
    tmp_path: Path,
) -> None:
    """serve_inherent must join the wake thread before closing the stream.

    Regression guard for the historical segfault: the shutdown finally block
    previously called wake_listener.request_stop() (non-blocking) and then
    immediately wake_stream.stop()/close(). The daemon thread is normally
    blocked inside stream.read(~80 ms/frame); closing the stream while it is
    mid-read is undefined PortAudio behaviour.

    This test verifies the call order: request_stop → join → stream.stop/close
    by placing instance-level spies on the listener and the fake stream, then
    executing the shutdown sequence and asserting the logged order.
    """
    call_log: list[str] = []

    fake_stream = MagicMock()
    fake_stream.read.return_value = (b"\x00" * 2560, False)
    fake_stream.stop.side_effect = lambda: call_log.append("stream.stop")
    fake_stream.close.side_effect = lambda: call_log.append("stream.close")

    pipeline = MagicMock()
    broadcaster = MagicMock()
    tts_pipeline = MagicMock()
    tts_pipeline.is_speaking = lambda: False

    with patch.object(voice_wake.WakeListener, "start"), \
         patch("jarvis.surface.voice_audio.SileroVad") as mock_silero, \
         patch.object(inherent_loop, "_open_wake_input_stream", return_value=fake_stream):
        mock_silero.return_value = MagicMock()
        _result = inherent_loop._spawn_wake_listener(
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=tmp_path / "silero.onnx",
            tts=tts_pipeline,
        )
        assert _result is not None
        listener, stream = _result
        assert stream is not None

    # Attach instance-level spies that survive outside the patch context.
    _real_request_stop = listener.request_stop
    _real_join = listener.join

    def _spy_request_stop() -> None:
        call_log.append("request_stop")
        _real_request_stop()

    def _spy_join(*, timeout_s: float | None = None) -> None:
        call_log.append("join")
        _real_join(timeout_s=timeout_s)

    listener.request_stop = _spy_request_stop  # type: ignore[method-assign]
    listener.join = _spy_join  # type: ignore[method-assign]

    # Drive the production shutdown helper directly — if someone reorders the
    # calls inside _shutdown_wake this test will catch it, unlike hand-rolling
    # the sequence here.
    inherent_loop._shutdown_wake(listener, stream)

    assert call_log == ["request_stop", "join", "stream.stop", "stream.close"], (
        f"shutdown call order wrong — got: {call_log!r}; "
        "join must happen after request_stop and before stream.stop/close"
    )


def test_shutdown_tts_closes_player() -> None:
    """``_shutdown_tts`` must call ``tts_pipe.close()`` to release the PortAudio device."""
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    tts_pipe = voice_tts.TTSPipeline(
        provider=MagicMock(spec=voice_tts.MiniMaxWSClient),
        player=player,
        fallback=lambda _text: None,
    )
    inherent_loop._shutdown_tts(tts_pipe)
    player.stop.assert_called_once()


def test_shutdown_tts_none_is_noop() -> None:
    """``_shutdown_tts(None)`` must not raise (voice subsystem may be absent)."""
    inherent_loop._shutdown_tts(None)  # must not raise
