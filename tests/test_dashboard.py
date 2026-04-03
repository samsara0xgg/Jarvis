"""TDD tests for dashboard controller — covers all user-facing scenarios."""

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import yaml
import pytest

from core.speech_recognizer import TranscriptionResult
from jarvis import JarvisApp
from ui.dashboard import DashboardController


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def ctrl(tmp_path):
    """Create a DashboardController with a real JarvisApp."""
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open() as f:
        config = yaml.safe_load(f)
    config.setdefault("auth", {})["user_store_path"] = str(tmp_path / "users.json")
    config["devices"]["mode"] = "sim"
    cp = tmp_path / "config.yaml"
    with cp.open("w") as f:
        yaml.safe_dump(config, f)
    app = JarvisApp(config, config_path=cp)
    return DashboardController(cp, app=app)


def _fake_audio(seconds=1.0, sr=16000):
    return (sr, np.random.randn(int(sr * seconds)).astype(np.float32))


def _mock_asr(ctrl, text):
    ctrl.app.speech_recognizer.transcribe = MagicMock(
        return_value=TranscriptionResult(text=text, language="zh", confidence=0.9)
    )

def _mock_verify_guest(ctrl):
    ctrl.app.speaker_verifier.verify = MagicMock(
        return_value=MagicMock(verified=False, confidence=0.0)
    )

def _mock_llm(ctrl, response="好的"):
    ctrl.app.llm.chat = MagicMock(return_value=(response, []))


# ------------------------------------------------------------------
# 1. Voice input
# ------------------------------------------------------------------

class TestHandleVoice:
    def test_none_audio_returns_empty(self, ctrl):
        assert ctrl.handle_voice(None) == ""

    def test_valid_audio_shows_asr_and_response(self, ctrl):
        _mock_asr(ctrl, "你好")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "你好！")
        result = ctrl.handle_voice(_fake_audio())
        assert "你好" in result
        assert "🤖" in result

    def test_empty_transcription(self, ctrl):
        _mock_asr(ctrl, "")
        assert "没听清" in ctrl.handle_voice(_fake_audio())

    def test_asr_crash(self, ctrl):
        ctrl.app.speech_recognizer.transcribe = MagicMock(side_effect=RuntimeError("boom"))
        assert "语音识别失败" in ctrl.handle_voice(_fake_audio())

    def test_llm_crash_fallback(self, ctrl):
        _mock_asr(ctrl, "天气")
        _mock_verify_guest(ctrl)
        ctrl.app.llm.chat = MagicMock(side_effect=RuntimeError("timeout"))
        ctrl.app.intent_router = None
        assert "云端服务不可用" in ctrl.handle_voice(_fake_audio())

    def test_verify_crash_falls_to_guest(self, ctrl):
        _mock_asr(ctrl, "你好")
        ctrl.app.speaker_verifier.verify = MagicMock(side_effect=RuntimeError("no model"))
        _mock_llm(ctrl, "你好！")
        assert "Guest" in ctrl.handle_voice(_fake_audio())

    def test_reqllm_falls_through_to_llm(self, ctrl):
        """When local executor returns REQLLM, should rephrase via LLM."""
        _mock_asr(ctrl, "你知道我是谁吗")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "我还不认识你呢，要不要注册一下？")

        from core.local_executor import Action, ActionResponse
        from unittest.mock import patch
        # Simulate intent router → info_query → REQLLM
        fake_route = MagicMock(
            tier="local", intent="info_query", sub_type="general",
            query="你知道我是谁吗", actions=[], response=None, rule=None,
        )
        ctrl.app.intent_router.route = MagicMock(return_value=fake_route)
        ctrl.app.local_executor.execute_info_query = MagicMock(
            return_value=ActionResponse(Action.REQLLM, "没查到相关信息。")
        )
        result = ctrl.handle_voice(_fake_audio())
        # Should use LLM rephrased response, NOT "没查到"
        assert "没查到" not in result
        assert "认识" in result or "注册" in result

    def test_garbage_local_response_falls_to_llm(self, ctrl):
        """Even non-REQLLM garbage like '没查到' should fallback to LLM."""
        _mock_asr(ctrl, "你知道我是谁吗")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "你好，我是小贾。")

        from core.local_executor import Action, ActionResponse
        fake_route = MagicMock(
            tier="local", intent="info_query", sub_type="general",
            query="你知道我是谁吗", actions=[], response=None, rule=None,
        )
        ctrl.app.intent_router.route = MagicMock(return_value=fake_route)
        ctrl.app.local_executor.execute_info_query = MagicMock(
            return_value=ActionResponse(Action.RESPONSE, "没查到相关信息。")
        )
        result = ctrl.handle_voice(_fake_audio())
        # Should detect garbage and use LLM instead
        assert "没查到" not in result

    def test_int16_audio(self, ctrl):
        _mock_asr(ctrl, "测试")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "收到")
        audio = (16000, (np.random.randn(16000) * 32767).astype(np.int16))
        assert "测试" in ctrl.handle_voice(audio)

    def test_stereo_audio(self, ctrl):
        _mock_asr(ctrl, "测试")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "收到")
        assert "测试" in ctrl.handle_voice((16000, np.random.randn(16000, 2).astype(np.float32)))

    def test_48khz_resampled(self, ctrl):
        _mock_asr(ctrl, "测试")
        _mock_verify_guest(ctrl)
        _mock_llm(ctrl, "收到")
        assert "测试" in ctrl.handle_voice((48000, np.random.randn(48000).astype(np.float32)))


# ------------------------------------------------------------------
# 2. Scenes
# ------------------------------------------------------------------

class TestScenes:
    def test_trigger_existing(self, ctrl):
        scenes = list(ctrl.config.get("automations", {}).keys())
        if not scenes:
            pytest.skip("No scenes")
        assert isinstance(ctrl.trigger_scene(scenes[0]), str)

    def test_trigger_nonexistent(self, ctrl):
        assert isinstance(ctrl.trigger_scene("不存在"), str)

    def test_trigger_no_engine(self, ctrl):
        ctrl.app.automation_engine = None
        assert "未启用" in ctrl.trigger_scene("test")


# ------------------------------------------------------------------
# 3. Rendering
# ------------------------------------------------------------------

class TestRendering:
    def test_header(self, ctrl):
        h = ctrl.render_header()
        assert "小贾" in h
        assert "sim" in h

    def test_devices(self, ctrl):
        html = ctrl.render_devices()
        assert "灯" in html or "锁" in html

    def test_health_with_tracker(self, ctrl):
        if not ctrl.app.health_tracker:
            pytest.skip("No tracker")
        ctrl.app.health_tracker.record_success("tts.openai")
        assert "正常" in ctrl.render_health()

    def test_health_no_tracker(self, ctrl):
        ctrl.app.health_tracker = None
        assert "未启用" in ctrl.render_health()

    def test_refresh_tuple(self, ctrl):
        r = ctrl.refresh()
        assert len(r) == 4
        assert all(isinstance(s, str) for s in r)


# ------------------------------------------------------------------
# 4. Audio coercion
# ------------------------------------------------------------------

class TestAudioCoercion:
    def test_invalid_raises(self, ctrl):
        with pytest.raises(ValueError):
            ctrl._coerce_audio("bad")

    def test_clips_range(self, ctrl):
        result = ctrl._coerce_audio((16000, np.ones(16000, dtype=np.float32) * 5))
        assert result.max() <= 1.0


# ------------------------------------------------------------------
# 5. Gradio build smoke test
# ------------------------------------------------------------------

class TestBuild:
    def test_build_ok(self, ctrl):
        try:
            import gradio
        except ImportError:
            pytest.skip("No gradio")
        from ui.dashboard import build_dashboard
        assert build_dashboard(controller=ctrl) is not None
