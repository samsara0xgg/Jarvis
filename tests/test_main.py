"""Tests for the main CLI pipeline orchestration in sim mode."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from auth.permission_manager import PermissionManager
from auth.user_store import UserStore
from core.command_parser import CommandParser
from core.speaker_verifier import VerificationResult
from core.speech_recognizer import TranscriptionResult
from devices.device_manager import DeviceManager
from main import SmartHomeVoiceLockApp
from tests.helpers import load_config


def _build_config(tmp_path: Path) -> dict:
    """Create a config copy with isolated user storage and sim-compatible aliases."""

    config = load_config()
    config.setdefault("auth", {})
    config["auth"]["user_store_path"] = str(tmp_path / "users.json")
    config.setdefault("devices", {})
    config["devices"]["mode"] = "sim"
    config.setdefault("hue", {})
    config["hue"]["light_aliases"] = {
        "bedroom_light": ["卧室灯", "卧室的灯"],
        "living_room_light": ["客厅灯", "客厅的灯"],
        "study_light": ["书房灯", "书房的灯"],
    }
    config["hue"]["group_aliases"] = {
        "living_room_group": ["客厅所有灯"],
    }
    return config


class _FakeAudioRecorder:
    """Fake recorder that returns a predefined waveform."""

    def __init__(self, audio: np.ndarray) -> None:
        """Store the waveform returned by the fake recorder."""

        self.audio = audio

    def record(self, duration: float | None = None) -> np.ndarray:
        """Return the predefined waveform regardless of duration."""

        del duration
        return self.audio

    def is_quality_ok(self, audio: np.ndarray) -> tuple[bool, str]:
        """Report every fake clip as valid."""

        del audio
        return True, "ok"


class _FakeSpeakerVerifier:
    """Fake speaker verifier that returns a fixed verification result."""

    def __init__(self, result: VerificationResult) -> None:
        """Store the verification result returned for all inputs."""

        self.result = result

    def verify(self, audio: np.ndarray) -> VerificationResult:
        """Ignore the audio and return the predefined verification result."""

        del audio
        return self.result


class _FakeSpeechRecognizer:
    """Fake ASR recognizer that returns fixed transcription output."""

    def __init__(self, result: TranscriptionResult) -> None:
        """Store the transcription result returned for all inputs."""

        self.result = result

    def transcribe(self, audio: np.ndarray) -> TranscriptionResult:
        """Ignore the audio and return the predefined transcription result."""

        del audio
        return self.result


class _FakeEnrollmentService:
    """Fake enrollment service used to test the registration command path."""

    def __init__(self) -> None:
        """Initialize the captured enrollment call list."""

        self.calls: list[dict[str, object]] = []

    def enroll_user(
        self,
        user_id: str,
        name: str,
        role: str | None = None,
        permissions: list[str] | None = None,
    ) -> dict[str, object]:
        """Capture enrollment requests and return a fake user record."""

        record = {
            "user_id": user_id,
            "name": name,
            "role": role or "resident",
            "permissions": permissions or [],
        }
        self.calls.append(record)
        return record


def _add_user(store: UserStore, user_id: str, name: str, role: str) -> None:
    """Add a valid user record to the JSON store."""

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


def test_process_audio_executes_sim_device_command(tmp_path: Path) -> None:
    """A verified spoken command should parse, pass permission, and execute."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    _add_user(user_store, "alice", "张三", "guest")
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(np.ones(16000, dtype=np.float32)),
        speaker_verifier=_FakeSpeakerVerifier(
            VerificationResult(
                verified=True,
                user="alice",
                confidence=0.89,
                all_scores={"alice": 0.89},
            )
        ),
        speech_recognizer=_FakeSpeechRecognizer(
            TranscriptionResult(
                text="打开客厅灯",
                language="zh",
                confidence=0.95,
            )
        ),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )

    result = app.process_audio(np.ones(16000, dtype=np.float32))

    assert result.permission_granted is True
    assert result.user_display_name == "张三"
    assert result.parsed_command == {"device": "living_room_light", "action": "turn_on"}
    assert "已打开" in result.execution_result
    assert app.device_manager.get_device("living_room_light").get_status()["is_on"] is True


def test_process_audio_rejects_when_verification_fails(tmp_path: Path) -> None:
    """Unverified speakers should be rejected before command execution."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(np.ones(16000, dtype=np.float32)),
        speaker_verifier=_FakeSpeakerVerifier(
            VerificationResult(
                verified=False,
                user=None,
                confidence=0.31,
                all_scores={"alice": 0.31},
            )
        ),
        speech_recognizer=_FakeSpeechRecognizer(
            TranscriptionResult(
                text="打开客厅灯",
                language="zh",
                confidence=0.95,
            )
        ),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )

    result = app.process_audio(np.ones(16000, dtype=np.float32))

    assert result.permission_granted is False
    assert result.parsed_command is None
    assert "声纹验证失败" in result.execution_result
    assert app.device_manager.get_device("living_room_light").get_status()["is_on"] is False


def test_process_audio_handles_register_special_command(tmp_path: Path) -> None:
    """The register command should trigger the enrollment flow."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    _add_user(user_store, "alice", "张三", "guest")
    enrollment_service = _FakeEnrollmentService()
    answers = iter(["bob", "李四", "member", "lights,unlock"])
    outputs: list[str] = []
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(np.ones(16000, dtype=np.float32)),
        speaker_verifier=_FakeSpeakerVerifier(
            VerificationResult(
                verified=True,
                user="alice",
                confidence=0.91,
                all_scores={"alice": 0.91},
            )
        ),
        speech_recognizer=_FakeSpeechRecognizer(
            TranscriptionResult(
                text="注册用户",
                language="zh",
                confidence=0.99,
            )
        ),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        enrollment_service=enrollment_service,
        input_func=lambda prompt: next(answers),
        output_func=outputs.append,
    )

    result = app.process_audio(np.ones(16000, dtype=np.float32))

    assert "注册成功" in result.execution_result
    assert enrollment_service.calls[0]["user_id"] == "bob"
    assert enrollment_service.calls[0]["permissions"] == ["lights", "unlock"]
    assert any("注册成功" in line for line in outputs)


def test_process_audio_handles_exit_special_command(tmp_path: Path) -> None:
    """The exit command should request loop termination."""

    config = _build_config(tmp_path)
    user_store = UserStore(config)
    app = SmartHomeVoiceLockApp(
        config,
        audio_recorder=_FakeAudioRecorder(np.ones(16000, dtype=np.float32)),
        speaker_verifier=_FakeSpeakerVerifier(
            VerificationResult(
                verified=False,
                user=None,
                confidence=0.10,
                all_scores={},
            )
        ),
        speech_recognizer=_FakeSpeechRecognizer(
            TranscriptionResult(
                text="退出",
                language="zh",
                confidence=0.99,
            )
        ),
        command_parser=CommandParser(config),
        device_manager=DeviceManager(config),
        permission_manager=PermissionManager(),
        user_store=user_store,
        input_func=lambda prompt: "",
        output_func=lambda message: None,
    )

    result = app.process_audio(np.ones(16000, dtype=np.float32))

    assert result.should_continue is False
    assert "退出命令" in result.execution_result
