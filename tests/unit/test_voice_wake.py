"""ADR-0005 voice_wake — wake listener orchestration (mocked openwakeword)."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from jarvis.surface import voice_pipeline, voice_wake


def _stop_and_assert_dead(listener: voice_wake.WakeListener) -> None:
    """Tell the listener to stop and fail loudly if it doesn't exit in time."""
    listener.request_stop()
    listener.join(timeout_s=1.0)
    assert not listener.is_alive(), "wake thread did not stop"


def test_wake_listener_calls_pipeline_on_detect() -> None:
    """When openwakeword fires, the listener captures + runs the pipeline."""
    fake_pipeline = MagicMock()
    fake_pipeline.run_turn.return_value = MagicMock(payload={"transcript": "你好"})
    fake_broadcaster = MagicMock()

    fake_engine = MagicMock()
    # First poll: detection (prob 0.9). Then return low values to stop further
    # detections (prevents repeated triggers).
    detections = iter([0.9, 0.0, 0.0, 0.0, 0.0])
    fake_engine.predict.side_effect = (
        lambda _frame: {"hey_jarvis_v0.1": next(detections, 0.0)}
    )

    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=fake_broadcaster,
        capture_callable=fake_capture,
        threshold=0.5,
    )
    listener.start()
    # Let it iterate a few times.
    time.sleep(0.1)
    _stop_and_assert_dead(listener)

    # Capture + pipeline both fired exactly on the detection iteration.
    fake_capture.assert_called()
    fake_pipeline.run_turn.assert_called_once()


def test_wake_listener_skips_when_tts_is_speaking() -> None:
    """Per ADR §2 no-barge-in rule: wake suspends while TTS is speaking."""
    fake_engine = MagicMock()
    fake_engine.predict.return_value = {"hey_jarvis_v0.1": 0.9}

    fake_pipeline = MagicMock()
    fake_broadcaster = MagicMock()
    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    tts_state = {"speaking": True}
    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=fake_broadcaster,
        capture_callable=fake_capture,
        threshold=0.5,
        is_speaking_callable=lambda: tts_state["speaking"],
    )
    listener.start()
    time.sleep(0.05)
    _stop_and_assert_dead(listener)
    fake_pipeline.run_turn.assert_not_called()
    fake_capture.assert_not_called()


def test_wake_listener_drops_detection_when_lock_busy() -> None:
    """If VOICE_INPUT_LOCK is held by PTT, wake detection is dropped (not blocked)."""
    fake_engine = MagicMock()
    fake_engine.predict.return_value = {"hey_jarvis_v0.1": 0.9}
    fake_pipeline = MagicMock()
    fake_broadcaster = MagicMock()
    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)

    voice_pipeline.VOICE_INPUT_LOCK.acquire()
    try:
        listener = voice_wake.WakeListener(
            engine=fake_engine,
            pipeline=fake_pipeline,
            broadcaster=fake_broadcaster,
            capture_callable=fake_capture,
            threshold=0.5,
        )
        listener.start()
        time.sleep(0.05)
        _stop_and_assert_dead(listener)
    finally:
        voice_pipeline.VOICE_INPUT_LOCK.release()
    fake_capture.assert_not_called()
    fake_pipeline.run_turn.assert_not_called()


def test_wake_listener_holds_lock_across_capture_phase() -> None:
    """Wake listener must hold VOICE_INPUT_LOCK across capture.

    ADR-0005 §8 fix #2 review: peek-and-release leaves the mic
    unguarded during capture; the lock must span the whole turn.
    """
    captured_lock_state: list[bool] = []
    capture_done = threading.Event()

    def _check_lock_held_during_capture() -> bytes:
        # The wake daemon thread should be holding the module-global lock
        # at this point — peek-and-release would show it as free.
        captured_lock_state.append(voice_pipeline.VOICE_INPUT_LOCK.locked())
        capture_done.set()
        return b"\x10\x00" * 16000

    fake_engine = MagicMock()
    # Only the first poll fires; subsequent polls below threshold so we
    # do not retrigger after the test signals stop.
    detections = iter([0.9, 0.0, 0.0, 0.0, 0.0])
    fake_engine.predict.side_effect = (
        lambda _frame: {"hey_jarvis_v0.1": next(detections, 0.0)}
    )

    fake_pipeline = MagicMock()
    fake_pipeline.run_turn.return_value = MagicMock(payload={"transcript": "你好"})

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=MagicMock(),
        capture_callable=_check_lock_held_during_capture,
        threshold=0.5,
    )
    listener.start()
    # Wait deterministically until capture has been called once.
    assert capture_done.wait(timeout=1.0), "capture_callable never fired"
    _stop_and_assert_dead(listener)

    # During capture, the lock MUST be held.
    assert any(captured_lock_state), (
        f"VOICE_INPUT_LOCK was NOT held during capture; states={captured_lock_state}"
    )
    # And run_turn must have been invoked with lock_already_held=True
    # (the listener owns the lock for the duration; the pipeline must not
    # try to re-acquire a non-reentrant Lock).
    _args, kwargs = fake_pipeline.run_turn.call_args
    assert kwargs.get("lock_already_held") is True, (
        f"run_turn must be called with lock_already_held=True; got kwargs={kwargs!r}"
    )
    # After the listener exited, the lock must be free again (no leak).
    assert not voice_pipeline.VOICE_INPUT_LOCK.locked(), (
        "wake listener leaked VOICE_INPUT_LOCK after shutdown"
    )


def test_engine_reset_called_when_capture_returns_none() -> None:
    """engine.reset() must run even when _capture_with_ducking returns None.

    Regression: previously the early `return` after `audio_bytes is None`
    skipped reset(), leaving openwakeword's 16-frame feature window primed
    near the wake threshold and causing a spurious re-fire on near-silence.
    """
    reset_done = threading.Event()

    fake_engine = MagicMock()
    # Fire on the first poll only; subsequent polls return 0.0.
    detections = iter([0.9, 0.0, 0.0, 0.0, 0.0])
    fake_engine.predict.side_effect = (
        lambda _frame: {"hey_jarvis_v0.1": next(detections, 0.0)}
    )
    # Signal the event without calling back into the mock (avoids recursion).
    fake_engine.reset.side_effect = lambda: reset_done.set()

    # Capture returns None — simulates mic disconnect / VAD timeout.
    fake_capture = MagicMock(return_value=None)
    fake_pipeline = MagicMock()

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=MagicMock(),
        capture_callable=fake_capture,
        threshold=0.5,
    )
    listener.start()
    assert reset_done.wait(timeout=1.0), "engine.reset() was not called after capture returned None"
    _stop_and_assert_dead(listener)

    fake_engine.reset.assert_called()
    # Pipeline must NOT have been invoked — capture failed.
    fake_pipeline.run_turn.assert_not_called()
    # Lock must not be leaked.
    assert not voice_pipeline.VOICE_INPUT_LOCK.locked(), (
        "VOICE_INPUT_LOCK leaked after capture-failure path"
    )


def test_engine_reset_called_exactly_once_on_success() -> None:
    """engine.reset() must be called exactly once on the successful capture path.

    Guards against a regression where reset() is double-called (e.g. once
    inside the pipeline try/finally and once in the outer finally).
    """
    reset_done = threading.Event()

    fake_engine = MagicMock()
    detections = iter([0.9, 0.0, 0.0, 0.0, 0.0])
    fake_engine.predict.side_effect = (
        lambda _frame: {"hey_jarvis_v0.1": next(detections, 0.0)}
    )
    # Signal without calling back into the mock (avoids infinite recursion).
    fake_engine.reset.side_effect = lambda: reset_done.set()

    fake_capture = MagicMock(return_value=b"\x10\x00" * 16000)
    fake_pipeline = MagicMock()
    fake_pipeline.run_turn.return_value = MagicMock(payload={"transcript": "你好"})

    listener = voice_wake.WakeListener(
        engine=fake_engine,
        pipeline=fake_pipeline,
        broadcaster=MagicMock(),
        capture_callable=fake_capture,
        threshold=0.5,
    )
    listener.start()
    assert reset_done.wait(timeout=1.0), "engine.reset() was not called on success path"
    _stop_and_assert_dead(listener)

    assert fake_engine.reset.call_count == 1, (
        f"engine.reset() called {fake_engine.reset.call_count} times; expected exactly 1"
    )
    # Lock must not be leaked.
    assert not voice_pipeline.VOICE_INPUT_LOCK.locked(), (
        "VOICE_INPUT_LOCK leaked after successful capture path"
    )
