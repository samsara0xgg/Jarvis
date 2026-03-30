"""Speaker verification utilities based on cosine similarity against enrolled templates."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

import numpy as np

from auth.user_store import UserStore
from core.speaker_encoder import SpeakerEncoder

LOGGER = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Structured result of a speaker verification attempt.

    Attributes:
        verified: Whether the best score cleared the configured threshold.
        user: The matched `user_id` when verification succeeds, otherwise `None`.
        confidence: The highest cosine similarity score found.
        all_scores: Cosine similarity scores for each enrolled user.
    """

    verified: bool
    user: str | None
    confidence: float
    all_scores: dict[str, float]


class SpeakerVerifier:
    """Verify a speaker by comparing an embedding against enrolled templates.

    Args:
        config: Parsed application configuration.
        speaker_encoder: Encoder used to convert audio into speaker embeddings.
        user_store: Persistent user template store.
    """

    def __init__(
        self,
        config: dict,
        speaker_encoder: SpeakerEncoder,
        user_store: UserStore,
    ) -> None:
        """Initialize the verifier with threshold and dependencies."""

        verification_config = config.get("verification", config)
        self.threshold = float(verification_config.get("threshold", 0.70))
        self.speaker_encoder = speaker_encoder
        self.user_store = user_store
        self.logger = LOGGER

    def verify(self, audio: np.ndarray) -> VerificationResult:
        """Verify an audio sample against all enrolled users.

        Args:
            audio: Input waveform to verify.

        Returns:
            The best-match verification result and score table.
        """

        users = self.user_store.get_all_users()
        if not users:
            self.logger.warning("No enrolled users available for verification.")
            return VerificationResult(
                verified=False,
                user=None,
                confidence=0.0,
                all_scores={},
            )

        query_embedding = self._normalize_embedding(self.speaker_encoder.encode(audio))
        scores: dict[str, float] = {}

        for user in users:
            user_id = str(user.get("user_id", "")).strip()
            if not user_id:
                self.logger.warning("Skipping user record without user_id: %s", user)
                continue

            try:
                enrolled_embedding = self._normalize_embedding(user.get("embedding", []))
            except ValueError as exc:
                self.logger.warning("Skipping invalid embedding for %s: %s", user_id, exc)
                continue

            if enrolled_embedding.shape != query_embedding.shape:
                self.logger.warning(
                    "Skipping %s due to embedding shape mismatch %s != %s.",
                    user_id,
                    enrolled_embedding.shape,
                    query_embedding.shape,
                )
                continue

            scores[user_id] = self._cosine_similarity(query_embedding, enrolled_embedding)

        if not scores:
            self.logger.warning("No valid enrolled speaker embeddings available for verification.")
            return VerificationResult(
                verified=False,
                user=None,
                confidence=0.0,
                all_scores={},
            )

        best_user, best_score = max(scores.items(), key=lambda item: item[1])
        verified = best_score >= self.threshold
        self.logger.info(
            "Speaker verification best match=%s score=%.4f threshold=%.4f verified=%s",
            best_user,
            best_score,
            self.threshold,
            verified,
        )
        return VerificationResult(
            verified=verified,
            user=best_user if verified else None,
            confidence=float(best_score),
            all_scores=scores,
        )

    def _normalize_embedding(self, embedding: Any) -> np.ndarray:
        """Convert an embedding to a normalized one-dimensional vector."""

        array = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if array.size == 0:
            raise ValueError("Speaker embedding is empty.")

        norm = float(np.linalg.norm(array))
        if norm == 0.0:
            raise ValueError("Speaker embedding has zero magnitude.")
        return array / norm

    def _cosine_similarity(self, left: np.ndarray, right: np.ndarray) -> float:
        """Compute cosine similarity for already normalized embeddings."""

        return float(np.dot(left, right))
