"""Tests for speaker verification scoring and threshold logic."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from auth.user_store import UserStore
from core.speaker_verifier import SpeakerVerifier
from tests.helpers import load_config


def _build_config(tmp_path: Path, threshold: float) -> dict:
    """Create a config copy with isolated storage and custom threshold."""

    config = load_config()
    config.setdefault("auth", {})
    config.setdefault("verification", {})
    config["auth"]["user_store_path"] = str(tmp_path / "users.json")
    config["verification"]["threshold"] = threshold
    return config


class _FakeSpeakerEncoder:
    """Fake encoder that returns a fixed embedding for verification."""

    def __init__(self, embedding: np.ndarray) -> None:
        """Store the embedding returned by every encode call."""

        self.embedding = embedding.astype(np.float32)

    def encode(self, audio: np.ndarray) -> np.ndarray:
        """Ignore the waveform and return the predefined embedding."""

        del audio
        return self.embedding


def _add_user(store: UserStore, user_id: str, embedding: list[float]) -> None:
    """Add a minimal valid user record to the store."""

    store.add_user(
        {
            "user_id": user_id,
            "name": user_id.title(),
            "embedding": embedding,
            "role": "resident",
            "permissions": ["unlock"],
            "enrolled_at": "2026-03-26T00:00:00+00:00",
        }
    )


def test_verifier_returns_best_matching_user(tmp_path: Path) -> None:
    """The verifier should select the highest-scoring enrolled user."""

    config = _build_config(tmp_path, threshold=0.70)
    store = UserStore(config)
    _add_user(store, "alice", [1.0, 0.0, 0.0])
    _add_user(store, "bob", [0.0, 1.0, 0.0])
    verifier = SpeakerVerifier(
        config,
        _FakeSpeakerEncoder(np.array([0.9, 0.1, 0.0], dtype=np.float32)),
        store,
    )

    result = verifier.verify(np.ones(16000, dtype=np.float32))

    assert result.verified is True
    assert result.user == "alice"
    assert result.confidence > 0.7
    assert result.all_scores["alice"] > result.all_scores["bob"]


def test_verifier_rejects_when_best_score_is_below_threshold(tmp_path: Path) -> None:
    """The verifier should reject speakers that do not reach the threshold."""

    config = _build_config(tmp_path, threshold=0.95)
    store = UserStore(config)
    _add_user(store, "alice", [1.0, 0.0, 0.0])
    _add_user(store, "bob", [0.0, 1.0, 0.0])
    verifier = SpeakerVerifier(
        config,
        _FakeSpeakerEncoder(np.array([0.6, 0.4, 0.0], dtype=np.float32)),
        store,
    )

    result = verifier.verify(np.ones(16000, dtype=np.float32))

    assert result.verified is False
    assert result.user is None
    assert result.confidence < 0.95
    assert set(result.all_scores) == {"alice", "bob"}


def test_verifier_rejects_when_no_users_are_enrolled(tmp_path: Path) -> None:
    """Verification should fail cleanly when the user store is empty."""

    config = _build_config(tmp_path, threshold=0.70)
    store = UserStore(config)
    verifier = SpeakerVerifier(
        config,
        _FakeSpeakerEncoder(np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        store,
    )

    result = verifier.verify(np.ones(16000, dtype=np.float32))

    assert result.verified is False
    assert result.user is None
    assert result.confidence == 0.0
    assert result.all_scores == {}


def test_verifier_skips_invalid_and_mismatched_user_embeddings(tmp_path: Path) -> None:
    """Malformed user records should be skipped instead of breaking verification."""

    class _FakeUserStore:
        def get_all_users(self) -> list[dict[str, object]]:
            return [
                {"user_id": "", "embedding": [1.0, 0.0, 0.0]},
                {"user_id": "broken", "embedding": []},
                {"user_id": "wrong-shape", "embedding": [1.0, 0.0]},
                {"user_id": "valid", "embedding": [1.0, 0.0, 0.0]},
            ]

    config = _build_config(tmp_path, threshold=0.70)
    verifier = SpeakerVerifier(
        config,
        _FakeSpeakerEncoder(np.array([1.0, 0.0, 0.0], dtype=np.float32)),
        _FakeUserStore(),
    )

    result = verifier.verify(np.ones(16000, dtype=np.float32))

    assert result.verified is True
    assert result.user == "valid"
    assert result.all_scores == {"valid": 1.0}
