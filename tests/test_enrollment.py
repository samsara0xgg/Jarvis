"""Tests for the speaker enrollment workflow."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from auth.enrollment import EnrollmentService
from auth.user_store import UserStore


def _load_config() -> dict:
    """Load the project config for enrollment tests."""

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def _build_config(tmp_path: Path) -> dict:
    """Create a test config with isolated storage and small enrollment settings."""

    config = _load_config()
    config.setdefault("auth", {})
    config.setdefault("enrollment", {})
    config["auth"]["user_store_path"] = str(tmp_path / "users.json")
    config["enrollment"]["num_samples"] = 3
    config["enrollment"]["max_attempts"] = 3
    return config


class _FakeRecorder:
    """Fake recorder that returns predefined audio clips."""

    def __init__(self, samples: list[np.ndarray]) -> None:
        """Store a queue of samples returned by subsequent record calls."""

        self.samples = list(samples)

    def record(self) -> np.ndarray:
        """Return the next audio clip."""

        return self.samples.pop(0)

    def is_quality_ok(self, audio: np.ndarray) -> tuple[bool, str]:
        """Treat all test samples as acceptable quality."""

        del audio
        return True, "ok"


class _FakeSpeakerEncoder:
    """Fake speaker encoder that emits predefined embeddings."""

    def __init__(self, embeddings: list[np.ndarray]) -> None:
        """Store the queue of embeddings returned by encode calls."""

        self.embeddings = list(embeddings)

    def encode(self, audio: np.ndarray) -> np.ndarray:
        """Return the next embedding regardless of audio content."""

        del audio
        return self.embeddings.pop(0)


def test_enrollment_averages_three_embeddings(tmp_path: Path) -> None:
    """Enrollment should average speaker embeddings into a normalized template."""

    config = _build_config(tmp_path)
    samples = [np.ones(16000, dtype=np.float32) for _ in range(3)]
    embeddings = [
        np.array([1.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0], dtype=np.float32),
        np.array([0.0, 0.0, 1.0], dtype=np.float32),
    ]
    recorder = _FakeRecorder(samples)
    encoder = _FakeSpeakerEncoder(embeddings)
    store = UserStore(config)
    enrollment_service = EnrollmentService(config, recorder, encoder, store)

    user_record = enrollment_service.enroll_user(
        user_id="alice",
        name="Alice",
        role="resident",
        permissions=["unlock"],
    )

    expected_embedding = (np.ones(3, dtype=np.float32) / np.sqrt(3.0)).tolist()
    assert user_record["user_id"] == "alice"
    assert user_record["permissions"] == ["unlock"]
    np.testing.assert_allclose(user_record["embedding"], expected_embedding, atol=1e-6)
    assert store.get_user("alice") is not None
