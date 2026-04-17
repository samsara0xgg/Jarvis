"""Tests for WP7 soft-stop state machine in InterruptMonitor."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.interrupt_monitor import InterruptMonitor


def _make_config(**overrides) -> dict:
    base = {
        "interrupt": {
            "enabled": True,
            "vad_model_path": "data/silero_vad.onnx",
            "vad_provider": "sherpa_onnx",
            "soft_stop_enabled": True,
            "soft_stop_timeout_ms": 200,  # short for tests
            "streaming_asr_chunk_samples": 100,
        }
    }
    base["interrupt"].update(overrides)
    return base


def _build_monitor(
    *,
    vad_speech_seq: list[bool],
    on_soft_pause=None,
    on_soft_resume=None,
    on_interrupt=None,
    config_overrides=None,
):
    """Build a monitor whose mocked VAD returns the given speech-detected sequence."""
    cfg = _make_config(**(config_overrides or {}))
    with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
         patch("sherpa_onnx.VadModelConfig"):
        mock_vad = MagicMock()
        idx = {"i": 0}

        def _is_speech_detected():
            i = idx["i"]
            idx["i"] = min(idx["i"] + 1, len(vad_speech_seq) - 1)
            return vad_speech_seq[i]

        mock_vad.is_speech_detected.side_effect = _is_speech_detected
        mock_vad_cls.return_value = mock_vad

        m = InterruptMonitor(
            config=cfg,
            on_interrupt=on_interrupt or (lambda: None),
            on_soft_pause=on_soft_pause,
            on_soft_resume=on_soft_resume,
        )
        m._vad = mock_vad
        m._recording = True
        return m


class TestSoftStopStateMachine:
    def test_vad_start_triggers_soft_pause(self):
        pause_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True],
            on_soft_pause=lambda: pause_calls.append(True),
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert pause_calls == [True]
        assert m._soft_state == "DUCKED"

    def test_no_pause_when_already_speech(self):
        # If first poll returns True, we transition NORMAL→DUCKED once.
        # A second feed with True should NOT re-fire pause.
        pause_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True, True],
            on_soft_pause=lambda: pause_calls.append(True),
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert pause_calls == [True]

    def test_vad_end_triggers_soft_resume(self):
        resume_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True, False],
            on_soft_pause=lambda: None,
            on_soft_resume=lambda: resume_calls.append(True),
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))  # ducks
        m.feed_audio(np.zeros(100, dtype=np.float32))  # resumes
        assert resume_calls == [True]
        assert m._soft_state == "NORMAL"

    def test_timeout_triggers_soft_resume(self):
        resume_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True],
            on_soft_pause=lambda: None,
            on_soft_resume=lambda: resume_calls.append(True),
            config_overrides={"soft_stop_timeout_ms": 100},
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert m._soft_state == "DUCKED"
        # Wait for the 100ms timer + a margin
        time.sleep(0.25)
        assert resume_calls == [True]
        assert m._soft_state == "NORMAL"

    def test_keyword_cancels_pending_timer(self):
        resume_calls: list[bool] = []
        interrupt_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True],
            on_soft_pause=lambda: None,
            on_soft_resume=lambda: resume_calls.append(True),
            on_interrupt=lambda: interrupt_calls.append(True),
            config_overrides={"soft_stop_timeout_ms": 200},
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert m._soft_state == "DUCKED"
        # Simulate keyword detection arriving from the streaming ASR.
        m._check_partial("停")
        assert interrupt_calls == [True]
        assert m._soft_state == "CANCELLED"
        # Timer should NOT fire after cancellation.
        time.sleep(0.35)
        assert resume_calls == []

    def test_disabled_soft_stop_skips_callbacks(self):
        pause_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True, False],
            on_soft_pause=lambda: pause_calls.append(True),
            on_soft_resume=lambda: pause_calls.append(False),
            config_overrides={"soft_stop_enabled": False},
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert pause_calls == []  # nothing fired

    def test_stop_resumes_if_left_ducked(self):
        # If we tear down while still DUCKED, caller's playback would be left
        # frozen — stop() must un-pause to avoid a hung process.
        resume_calls: list[bool] = []
        m = _build_monitor(
            vad_speech_seq=[True, True],  # stays in speech
            on_soft_pause=lambda: None,
            on_soft_resume=lambda: resume_calls.append(True),
            config_overrides={"soft_stop_timeout_ms": 5000},
        )
        m.feed_audio(np.zeros(100, dtype=np.float32))
        assert m._soft_state == "DUCKED"
        m.stop()
        assert resume_calls == [True]


class TestTimerStopRace:
    """WP7 T2.4: when stop() and the soft-resume timer race, on_soft_resume
    should still be called at most once (the resume op is idempotent, but
    we don't want duplicate log noise either).

    Synchronized — no wall-clock sleeps. We reach into the monitor's
    `_soft_resume_timer` and invoke `_on_soft_timeout` directly to simulate
    the timer firing at a chosen moment.
    """

    def _make_monitor(self, on_soft_resume, on_soft_pause=None):
        return InterruptMonitor(
            config={
                "interrupt": {
                    "enabled": True,
                    "soft_stop_enabled": True,
                    "soft_stop_timeout_ms": 100,
                }
            },
            on_soft_pause=on_soft_pause or (lambda: None),
            on_soft_resume=on_soft_resume,
        )

    def test_stop_wins_race_timer_callback_noops(self):
        resume_calls: list[int] = []

        monitor = self._make_monitor(
            on_soft_resume=lambda: resume_calls.append(1),
            on_soft_pause=lambda: None,
        )
        monitor.start()

        # Force state into DUCKED by simulating a VAD start edge.
        monitor._update_soft_state(is_speech=True)
        timer_obj = monitor._soft_resume_timer
        assert timer_obj is not None

        # Main thread wins the race: stop() called first.
        monitor.stop()
        # After stop(), state is NORMAL; on_soft_resume fired once (by stop).
        assert len(resume_calls) == 1

        # Now simulate the timer actually firing (as if it was already
        # queued when we called stop). _on_soft_timeout must see NORMAL
        # and NOT call on_soft_resume again.
        monitor._on_soft_timeout()
        assert len(resume_calls) == 1, (
            f"timer callback must no-op after stop() already resumed; "
            f"got {len(resume_calls)} calls"
        )

    def test_timer_wins_race_stop_noops_further(self):
        resume_calls: list[int] = []

        monitor = self._make_monitor(
            on_soft_resume=lambda: resume_calls.append(1),
            on_soft_pause=lambda: None,
        )
        monitor.start()

        # Force DUCKED
        monitor._update_soft_state(is_speech=True)
        assert monitor._soft_state == "DUCKED"

        # Timer wins: simulate its callback firing first.
        monitor._on_soft_timeout()
        assert len(resume_calls) == 1
        assert monitor._soft_state == "NORMAL"

        # stop() now runs; state is already NORMAL so it must NOT
        # call on_soft_resume again.
        monitor.stop()
        assert len(resume_calls) == 1, (
            "stop() must no-op on_soft_resume when state is NORMAL"
        )
