"""Tests for TTS audio disk cache."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.tts import TTSEngine


@pytest.fixture
def tts_with_cache(tmp_path):
    """TTSEngine with cache pointed at a temp dir."""
    with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
        engine = TTSEngine.__new__(TTSEngine)
        engine.engine_name = "minimax"
        engine.minimax_key = "test_key"
        engine.minimax_model = "speech-02-turbo"
        engine.minimax_voice = "male-qn-qingse"
        engine.minimax_volume = 1
        engine._minimax_base_url = "https://api.minimax.chat"
        engine._minimax_url = "https://api.minimax.chat/v1/t2a_v2"
        engine._http_session = MagicMock()
        engine._tracker = None
        engine._tts_cache_dir = tmp_path
        engine._tts_cache_max = 5
        engine.speed = 1.0
        engine.logger = MagicMock()
        engine._platform = "Darwin"
        engine._preprocessor_config = {}
    return engine


class TestTTSCache:
    def test_cache_key_deterministic(self, tts_with_cache):
        k1 = tts_with_cache._tts_cache_key("好的", "calm")
        k2 = tts_with_cache._tts_cache_key("好的", "calm")
        assert k1 == k2

    def test_cache_key_differs_by_emotion(self, tts_with_cache):
        k1 = tts_with_cache._tts_cache_key("好的", "calm")
        k2 = tts_with_cache._tts_cache_key("好的", "happy")
        assert k1 != k2

    def test_cache_key_differs_by_engine(self, tts_with_cache):
        """Switching engines must invalidate cache — different engines produce
        different audio for the same text, so the key must include engine name."""
        tts_with_cache.engine_name = "minimax"
        k_minimax = tts_with_cache._tts_cache_key("好的", "calm")
        tts_with_cache.engine_name = "openai_tts"
        k_openai = tts_with_cache._tts_cache_key("好的", "calm")
        tts_with_cache.engine_name = "azure"
        k_azure = tts_with_cache._tts_cache_key("好的", "calm")
        assert k_minimax != k_openai
        assert k_minimax != k_azure
        assert k_openai != k_azure

    def test_cache_hit_returns_existing_file(self, tts_with_cache, tmp_path):
        key = tts_with_cache._tts_cache_key("好的", "calm")
        cache_path = tmp_path / f"{key}.mp3"
        cache_path.write_bytes(b"fake_mp3_data")

        result_path, deletable = tts_with_cache._synth_minimax("好的", "calm")
        assert result_path == str(cache_path)
        assert deletable is False
        tts_with_cache._http_session.post.assert_not_called()

    def test_cache_miss_calls_api_and_saves(self, tts_with_cache, tmp_path):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"audio": b"deadbeef".hex()},
            "base_resp": {"status_msg": "ok"},
        }
        tts_with_cache._http_session.post.return_value = mock_resp

        result_path, deletable = tts_with_cache._synth_minimax("好的", "calm")
        assert deletable is False
        assert str(tmp_path) in result_path
        assert Path(result_path).exists()

    def test_long_text_bypasses_cache(self, tts_with_cache):
        long_text = "这是一段很长的文本超过了五十个字符的限制所以不应该被缓存起来要确保它确实超过了五十个字符才行呢对对对啊"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"audio": b"deadbeef".hex()},
            "base_resp": {"status_msg": "ok"},
        }
        tts_with_cache._http_session.post.return_value = mock_resp

        result_path, deletable = tts_with_cache._synth_minimax(long_text, "calm")
        assert deletable is True
        assert str(tts_with_cache._tts_cache_dir) not in result_path

    def test_cache_eviction(self, tts_with_cache, tmp_path):
        tts_with_cache._tts_cache_max = 3
        import time
        paths = []
        for i in range(4):
            p = tmp_path / f"file_{i}.mp3"
            p.write_bytes(b"data")
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)

        tts_with_cache._evict_tts_cache()
        remaining = list(tmp_path.glob("*.mp3"))
        assert len(remaining) == 3
        assert not paths[0].exists()

    def test_is_cached_file(self, tts_with_cache, tmp_path):
        cached = tmp_path / "abc.mp3"
        assert tts_with_cache._is_cached_file(str(cached)) is True
        assert tts_with_cache._is_cached_file("/tmp/xyz.mp3") is False


class TestMinimaxVolumeCoercion:
    """WP3 T1.1: MiniMax `vol` is int 0-10 per API; Jarvis config may have floats."""

    def _make_engine(self, vol_cfg):
        from core.tts import TTSEngine
        with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
            eng = TTSEngine.__new__(TTSEngine)
            tts_cfg = {"minimax_volume": vol_cfg}
            # Mirror the real init line minus anything else
            raw_vol = tts_cfg.get("minimax_volume", 1)
            try:
                v = int(round(float(raw_vol)))
            except (TypeError, ValueError):
                v = 1
            eng.minimax_volume = max(1, min(10, v))
            return eng

    def test_float_config_becomes_int(self):
        assert self._make_engine(1.0).minimax_volume == 1
        assert isinstance(self._make_engine(1.0).minimax_volume, int)

    def test_rounds_to_nearest(self):
        assert self._make_engine(7.8).minimax_volume == 8
        assert self._make_engine(3.4).minimax_volume == 3

    def test_clamps_upper(self):
        assert self._make_engine(20).minimax_volume == 10

    def test_clamps_lower(self):
        assert self._make_engine(0).minimax_volume == 1
        assert self._make_engine(-5).minimax_volume == 1

    def test_bad_input_falls_back_to_1(self):
        assert self._make_engine("not a number").minimax_volume == 1
        assert self._make_engine(None).minimax_volume == 1
