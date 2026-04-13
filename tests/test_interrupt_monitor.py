"""Tests for InterruptMonitor — streaming ASR keyword detection."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.interrupt_monitor import (
    InterruptMonitor,
    INTERRUPT_KEYWORDS,
    RESUME_KEYWORDS,
    strip_interrupt_prefix,
)


class TestInterruptMonitorKeywordMatch:
    def test_detects_interrupt_keyword_in_partial(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        assert len(detected) == 1

    def test_ignores_non_keyword(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("明天天气")
        assert len(detected) == 0

    def test_detects_keyword_as_substring(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停改成多伦多")
        assert len(detected) == 1

    def test_detects_resume_keyword(self):
        resume_detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_resume=lambda: resume_detected.append("resume"),
        )
        monitor._check_partial("继续说")
        assert len(resume_detected) == 1

    def test_fires_only_once_per_session(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        monitor._check_partial("停停停")
        assert len(detected) == 1

    def test_reset_allows_new_detection(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        assert len(detected) == 1
        monitor.reset()
        monitor._check_partial("停")
        assert len(detected) == 2


class TestInterruptMonitorAudio:
    def test_feed_audio_accepts_float32_array(self):
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        audio = np.zeros(1600, dtype=np.float32)
        monitor.feed_audio(audio, sample_rate=16000)

    def test_disabled_monitor_does_nothing(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": False}},
            on_interrupt=lambda: detected.append("x"),
        )
        monitor._check_partial("停")
        assert len(detected) == 0
