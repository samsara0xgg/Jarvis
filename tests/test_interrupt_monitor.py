"""Tests for InterruptMonitor — VAD-segmented SenseVoice keyword detection."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np

from core.interrupt_monitor import (
    INTERRUPT_KEYWORDS,
    RESUME_KEYWORDS,
    InterruptMonitor,
    strip_interrupt_prefix,
)


def _make_recognizer(text: str = "") -> MagicMock:
    """Build a SpeechRecognizer-compatible mock returning `text`."""
    mock = MagicMock()
    mock.transcribe.return_value = MagicMock(text=text)
    return mock


def _make_normalizer() -> MagicMock:
    """Pass-through ASRNormalizer mock."""
    mock = MagicMock()
    mock.normalize.side_effect = lambda t: t
    return mock


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


class TestMicListener:
    def test_start_stop_mic_listener(self):
        """Mic listener should start/stop without errors (mocked sounddevice)."""
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
            speech_recognizer=_make_recognizer(),
            asr_normalizer=_make_normalizer(),
        )
        mock_stream = MagicMock()
        mock_stream.read.return_value = (np.zeros((1600, 1), dtype="float32"), None)
        with patch("sounddevice.InputStream", return_value=mock_stream):
            monitor.start()
            monitor.start_mic_listener()
            time.sleep(0.2)
            monitor.stop_mic_listener()
            monitor.stop()
            mock_stream.start.assert_called_once()
            mock_stream.stop.assert_called_once()


class TestInterruptMonitorVADGate:
    """With VAD present, only ACTIVE→IDLE edges dispatch transcription."""

    def _make_config(self, **overrides):
        base = {
            "interrupt": {
                "enabled": True,
                "vad_model_path": "data/silero_vad.onnx",
                "vad_provider": "sherpa_onnx",
                "vad_threshold_during_tts": 0.8,
                "vad_min_speech_duration": 0.15,
                "vad_min_silence_duration": 0.2,
                "vad_max_speech_duration": 10.0,
                # Very small threshold so a single 1600-sample chunk
                # crosses it and a VAD close flushes to transcribe.
                "min_segment_ms": 10,
            }
        }
        base["interrupt"].update(overrides)
        return base

    def test_vad_loaded_when_enabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig") as mock_cfg_cls:
            mock_cfg = MagicMock()
            mock_cfg.silero_vad = MagicMock()
            mock_cfg_cls.return_value = mock_cfg

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
                speech_recognizer=_make_recognizer(),
                asr_normalizer=_make_normalizer(),
            )
            monitor.start()

            assert mock_vad_cls.called
            assert mock_cfg.silero_vad.model == "data/silero_vad.onnx"
            assert mock_cfg.silero_vad.threshold == 0.8
            assert mock_cfg.silero_vad.min_speech_duration == 0.15
            assert mock_cfg.silero_vad.min_silence_duration == 0.2

    def test_no_transcribe_when_vad_never_opens(self):
        """If VAD is silent the whole time, SpeechRecognizer is never called."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad.is_speech_detected.return_value = False
            mock_vad_cls.return_value = mock_vad

            rec = _make_recognizer(text="")
            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
                speech_recognizer=rec,
                asr_normalizer=_make_normalizer(),
            )
            monitor.start()

            audio = np.zeros(1600, dtype=np.float32)
            monitor.feed_audio(audio)
            monitor.feed_audio(audio)
            monitor.stop()

            rec.transcribe.assert_not_called()

    def test_vad_close_dispatches_transcribe(self):
        """VAD ACTIVE→IDLE edge flushes the segment to SpeechRecognizer."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            # First chunk: speech. Second chunk: silence (close edge).
            mock_vad.is_speech_detected.side_effect = [True, False]
            mock_vad_cls.return_value = mock_vad

            fires: list[str] = []
            rec = _make_recognizer(text="停")
            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: fires.append("int"),
                speech_recognizer=rec,
                asr_normalizer=_make_normalizer(),
            )
            monitor.start()

            audio = np.ones(1600, dtype=np.float32) * 0.1
            monitor.feed_audio(audio)  # VAD opens, audio accumulated
            monitor.feed_audio(audio)  # VAD closes → dispatch transcribe
            monitor.stop()  # waits for worker

            rec.transcribe.assert_called_once()
            assert fires == ["int"]


class TestStopPreventsFurtherCallbacks:
    """WP7 T2.3: after stop(), feed_audio must not fire callbacks."""

    def test_callback_not_fired_after_stop(self):
        fires: list[str] = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: fires.append("int"),
            speech_recognizer=_make_recognizer(),
            asr_normalizer=_make_normalizer(),
        )
        monitor.start()
        monitor.stop()
        # Simulate a late-arriving chunk on the mic thread
        monitor.feed_audio(np.zeros(1600, dtype=np.float32))
        # Post-stop: no accumulation, no callback.
        assert monitor._audio_chunks == []
        assert fires == []

    def test_start_clears_fired_under_lock(self):
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
            speech_recognizer=_make_recognizer(),
            asr_normalizer=_make_normalizer(),
        )
        monitor._fired = True  # stale
        monitor.start()
        assert monitor._fired is False


class TestNormalizerApplied:
    """B4: three-layer ASRNormalizer runs on interrupt transcriptions too."""

    def test_normalizer_output_used_for_keyword_match(self):
        """If normalizer rewrites 'tin' → '停', the keyword should fire."""
        fires: list[str] = []
        rec = _make_recognizer(text="tin")
        normalizer = MagicMock()
        normalizer.normalize.side_effect = lambda t: "停" if t == "tin" else t

        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: fires.append("int"),
            speech_recognizer=rec,
            asr_normalizer=normalizer,
        )
        # Drive the transcribe path directly (no VAD) to isolate the
        # normalizer contract.
        monitor._transcribe_segment(np.zeros(8000, dtype=np.float32))

        normalizer.normalize.assert_called_once_with("tin")
        assert fires == ["int"]
