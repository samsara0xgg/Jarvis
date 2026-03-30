"""Tests for the dashboard controller logic without requiring Gradio."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from auth.permission_manager import PermissionManager
from auth.user_store import UserStore
from core.command_parser import CommandParser
from core.speaker_verifier import VerificationResult
from core.speech_recognizer import TranscriptionResult
from devices.device_manager import DeviceManager
from main import SmartHomeVoiceLockApp
from ui.dashboard import DashboardController


def _load_config() -> dict:
    """Load the project config for dashboard tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def _build_config(tmp_path: Path) -> dict:
    """Create an isolated config copy for dashboard tests."""

    config = _load_config()
    config.setdefault("auth", {})
    config["auth"]["user_store_path"] = str(tmp_path / "users.json")
    config.setdefault("devices", {})
    config["devices"]["mode"] = "sim"
    return config


class _FakeAudioRecorder:
    """Fake recorder used for pipeline and enrollment tests."""

    def is_quality_ok(self, audio: np.ndarray) -> tuple[bool, str]:
        """Treat all samples as valid."""

        del audio
        return True, "ok"


class _FakeSpeakerEncoder:
    """Fake speaker encoder with deterministic outputs."""

    def __init__(self) -> None:
        """Initialize the encoder call counter."""

        self.calls = 0

    def encode(self, audio: np.ndarray) -> np.ndarray:
        """Return a simple deterministic embedding."""

        del audio
        self.calls += 1
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


class _FakeSpeakerVerifier:
    """Fake verifier that always authenticates the same user."""

    def verify(self, audio: np.ndarray) -> VerificationResult:
        """Ignore the waveform and return a successful verification result."""

        del audio
        return VerificationResult(
            verified=True,
            user="alice",
            confidence=0.88,
            all_scores={"alice": 0.88},
        )


class _FakeSpeechRecognizer:
    """Fake ASR recognizer returning a configured sentence."""

    def __init__(self, text: str) -> None:
        """Store the transcription text returned by the fake recognizer."""

        self.text = text

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """Ignore the waveform and return the configured transcription."""

        del audio
        return TranscriptionResult(text=self.text, language="zh", confidence=0.95)


def _add_user(store: UserStore, user_id: str, name: str, role: str) -> None:
    """Add a user to the JSON store."""

    store.add_user(
        {
            "user_id": user_id,
            "name": name,
            "embedding": [0.1, 0.2, 0.3],
            "role": role,
            "permissions": ["unlock"],
            "enrolled_at": "2026-03-26T00:00:00+00:00",
        }
    )


def test_dashboard_controller_updates_pipeline_and_device_panel(tmp_path: Path) -> None:
    """Voice handling should update the pipeline panel and simulated device state."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    _add_user(user_store, "alice", "张三", "guest")
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(),
        speaker_encoder=_FakeSpeakerEncoder(),
        speaker_verifier=_FakeSpeakerVerifier(),
        speech_recognizer=_FakeSpeechRecognizer("打开客厅灯"),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )
    controller = DashboardController(tmp_path / "config.yaml", app=app)

    pipeline_html, device_html, header_html, logs_text = controller.handle_voice_input(
        (16000, np.ones(16000, dtype=np.float32))
    )

    assert "客厅灯 已打开" in pipeline_html or "客厅灯已打开" in pipeline_html
    assert "living_room_light" in device_html
    assert "模式: sim" in header_html
    assert "打开客厅灯" in logs_text


def test_dashboard_controller_registers_user_from_three_samples(tmp_path: Path) -> None:
    """Dashboard registration should reuse enrollment flow with browser samples."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(),
        speaker_encoder=_FakeSpeakerEncoder(),
        speaker_verifier=_FakeSpeakerVerifier(),
        speech_recognizer=_FakeSpeechRecognizer("打开客厅灯"),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )
    controller = DashboardController(tmp_path / "config.yaml", app=app)

    message, user_rows, _, _ = controller.register_user_from_samples(
        "bob",
        "李四",
        "member",
        "lights,unlock",
        (16000, np.ones(16000, dtype=np.float32)),
        (16000, np.ones(16000, dtype=np.float32)),
        (16000, np.ones(16000, dtype=np.float32)),
    )

    assert "注册成功" in message
    assert any(row[0] == "bob" and row[1] == "李四" for row in user_rows)
