"""Tests for core.vad_silero (WP6).

Mocks onnxruntime so tests don't need the real ONNX model loaded; one
integration-style test does load the real model if available, to catch
shape/signature regressions against the bundled ``data/silero_vad.onnx``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.vad_silero import SileroVADDirect, _CHUNK_SAMPLES, build_vad


@pytest.fixture
def mock_session_factory():
    """Return a callable that patches ort.InferenceSession with a controllable mock."""
    def _make(prob_seq):
        """prob_seq: list of probs to return (cycles if exhausted)."""
        sess = MagicMock()
        idx = {"i": 0}

        def _run(_outputs, feeds):
            i = idx["i"] % len(prob_seq)
            idx["i"] += 1
            prob = np.array([[prob_seq[i]]], dtype=np.float32)
            new_h = np.zeros((2, 1, 64), dtype=np.float32)
            new_c = np.zeros((2, 1, 64), dtype=np.float32)
            return [prob, new_h, new_c]

        sess.run.side_effect = _run
        sess._reset_idx = lambda: idx.update(i=0)
        return sess

    return _make


def _build(mock_session, **overrides) -> SileroVADDirect:
    kwargs = dict(
        model_path="/dev/null",
        prob_threshold=0.5,
        db_threshold=-200.0,  # disable dB gate by default for unit tests
        smoothing_window=1,
        required_hits=2,
        required_misses=3,
    )
    kwargs.update(overrides)
    with patch("onnxruntime.InferenceSession", return_value=mock_session):
        vad = SileroVADDirect(**kwargs)
    # Hide warm-up bookkeeping so tests can reason about a clean mock.
    mock_session._reset_idx()
    mock_session.run.reset_mock()
    return vad


class TestStateMachine:
    def test_silence_does_not_trigger(self, mock_session_factory):
        sess = mock_session_factory([0.1] * 50)  # all silence
        vad = _build(sess)
        for _ in range(10):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.is_speech_detected() is False
        assert vad.empty() is True

    def test_speech_triggers_after_required_hits(self, mock_session_factory):
        sess = mock_session_factory([0.9] * 50)  # all speech
        vad = _build(sess, required_hits=3)
        # Feed 3 chunks → enters ACTIVE
        for _ in range(3):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.is_speech_detected() is True

    def test_single_high_frame_does_not_trigger(self, mock_session_factory):
        # First frame above, next two below → never reaches ACTIVE
        sess = mock_session_factory([0.9, 0.1, 0.1, 0.1, 0.1, 0.1])
        vad = _build(sess, required_hits=3)
        for _ in range(5):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.is_speech_detected() is False

    def test_segment_completed_after_misses(self, mock_session_factory):
        # 3 hits → ACTIVE, then 3 misses → IDLE + segment_completed
        seq = [0.9] * 3 + [0.1] * 5
        sess = mock_session_factory(seq)
        vad = _build(sess, required_hits=3, required_misses=3)
        for _ in range(8):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.is_speech_detected() is False
        assert vad.empty() is False  # a segment has been completed


class TestStateReset:
    def test_reset_clears_segment_flag(self, mock_session_factory):
        seq = [0.9] * 3 + [0.1] * 5
        sess = mock_session_factory(seq)
        vad = _build(sess, required_hits=3, required_misses=3)
        for _ in range(8):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.empty() is False
        vad.reset()
        assert vad.empty() is True
        assert vad.is_speech_detected() is False

    def test_reset_clears_lstm_state(self, mock_session_factory):
        sess = mock_session_factory([0.5])
        vad = _build(sess)
        vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        # State should now be all-zero (the mock returns zeros), but reset
        # should also be safe and not raise.
        vad.reset()
        assert np.all(vad._h == 0)
        assert np.all(vad._c == 0)


class TestChunking:
    def test_handles_short_input_buffers(self, mock_session_factory):
        sess = mock_session_factory([0.1])
        vad = _build(sess)
        # Feed 100 samples (< chunk size) — must not call run, must not crash
        vad.accept_waveform(np.zeros(100, dtype=np.float32))
        assert sess.run.call_count == 0

    def test_handles_oversize_input(self, mock_session_factory):
        sess = mock_session_factory([0.1] * 10)
        vad = _build(sess)
        # 1000 samples → 1 full chunk (488 buffered for next call); warm-up
        # already excluded by the fixture.
        vad.accept_waveform(np.zeros(1000, dtype=np.float32))
        assert sess.run.call_count == 1

    def test_concatenates_buffer_across_calls(self, mock_session_factory):
        sess = mock_session_factory([0.1] * 10)
        vad = _build(sess)
        before = sess.run.call_count
        vad.accept_waveform(np.zeros(300, dtype=np.float32))
        vad.accept_waveform(np.zeros(300, dtype=np.float32))  # total 600 → 1 chunk
        assert sess.run.call_count - before == 1
        vad.accept_waveform(np.zeros(424, dtype=np.float32))  # total now 1024 → 1 more chunk
        assert sess.run.call_count - before == 2


class TestDbGate:
    def test_high_prob_low_db_does_not_trigger(self, mock_session_factory):
        # Audio is silence (zeros) → dB == -200 floor; even with prob 0.9
        # the AND-gate keeps state IDLE.
        sess = mock_session_factory([0.9] * 10)
        vad = _build(sess, db_threshold=-30.0, required_hits=3)
        for _ in range(5):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        assert vad.is_speech_detected() is False

    def test_high_prob_and_high_db_triggers(self, mock_session_factory):
        # Audio is loud noise → dB clears the gate; prob 0.9 → ACTIVE.
        sess = mock_session_factory([0.9] * 10)
        vad = _build(sess, db_threshold=-100.0, required_hits=3)
        loud = np.full(_CHUNK_SAMPLES, 0.5, dtype=np.float32)
        for _ in range(5):
            vad.accept_waveform(loud)
        assert vad.is_speech_detected() is True


class TestProviderFactory:
    def test_factory_returns_silero_direct(self, mock_session_factory):
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
        }
        with patch("onnxruntime.InferenceSession", return_value=mock_session_factory([0.1])):
            inst = build_vad(cfg, mode="record")
            assert isinstance(inst, SileroVADDirect)

    def test_factory_tts_mode_uses_tts_thresholds(self, mock_session_factory):
        cfg = {
            "vad_provider": "silero_direct",
            "vad_model_path": "/dev/null",
            "vad_prob_threshold_during_tts": 0.7,
            "vad_db_threshold_during_tts_mac": 80.0,
            "vad_db_threshold_during_tts_rpi": 65.0,
        }
        with patch("onnxruntime.InferenceSession", return_value=mock_session_factory([0.1])):
            with patch("platform.system", return_value="Darwin"):
                inst = build_vad(cfg, mode="tts")
                assert inst._db_threshold == 80.0
            with patch("platform.system", return_value="Linux"):
                inst2 = build_vad(cfg, mode="tts")
                assert inst2._db_threshold == 65.0


class TestRealModel:
    """Integration-ish: load the real bundled ONNX file if present."""

    @pytest.fixture
    def real_model_path(self) -> Path:
        p = Path(__file__).resolve().parent.parent / "data" / "silero_vad.onnx"
        if not p.exists():
            pytest.skip("data/silero_vad.onnx not present")
        return p

    def test_loads_and_warms_up(self, real_model_path):
        # Just ensure construction works without raising on the real model.
        vad = SileroVADDirect(model_path=str(real_model_path))
        assert vad.is_speech_detected() is False
        assert vad.empty() is True

    def test_silence_returns_low_prob(self, real_model_path):
        vad = SileroVADDirect(
            model_path=str(real_model_path),
            db_threshold=-200.0,  # bypass dB gate so we test prob path
            required_hits=10,
        )
        for _ in range(20):
            vad.accept_waveform(np.zeros(_CHUNK_SAMPLES, dtype=np.float32))
        # Pure zeros must not look like speech to silero.
        assert vad.is_speech_detected() is False
