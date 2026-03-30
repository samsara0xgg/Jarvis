"""Enrollment workflow for collecting voice samples and building speaker templates."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

import numpy as np

from auth.user_store import UserStore
from core.audio_recorder import AudioRecorder
from core.speaker_encoder import SpeakerEncoder

LOGGER = logging.getLogger(__name__)


class EnrollmentService:
    """Enroll users by averaging multiple speaker embeddings into one template.

    Args:
        config: Parsed application configuration.
        audio_recorder: Recorder used to capture enrollment utterances.
        speaker_encoder: Encoder used to extract speaker embeddings.
        user_store: Persistent user profile store.
    """

    def __init__(
        self,
        config: dict,
        audio_recorder: AudioRecorder,
        speaker_encoder: SpeakerEncoder,
        user_store: UserStore,
    ) -> None:
        """Initialize the enrollment workflow and its dependencies."""

        enrollment_config = config.get("enrollment", config)
        self.samples_required = int(enrollment_config.get("num_samples", 3))
        self.max_attempts = int(enrollment_config.get("max_attempts", max(3, self.samples_required)))
        self.default_role = str(enrollment_config.get("default_role", "resident"))
        self.default_permissions = [
            str(permission) for permission in enrollment_config.get("default_permissions", [])
        ]

        self.audio_recorder = audio_recorder
        self.speaker_encoder = speaker_encoder
        self.user_store = user_store
        self.logger = LOGGER

    def enroll_user(
        self,
        user_id: str,
        name: str,
        role: str | None = None,
        permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Collect enrollment utterances and persist an averaged voice template.

        Args:
            user_id: Unique user identifier.
            name: Human-readable display name.
            role: Role to store with the user profile.
            permissions: Permission strings granted to the user.

        Returns:
            The stored user record.

        Raises:
            RuntimeError: If enough high-quality samples cannot be collected.
        """

        effective_role = role or self.default_role
        effective_permissions = (
            [str(permission) for permission in permissions]
            if permissions is not None
            else list(self.default_permissions)
        )

        embeddings: list[np.ndarray] = []
        attempts = 0

        while len(embeddings) < self.samples_required and attempts < self.max_attempts:
            attempts += 1
            target_sample_number = len(embeddings) + 1
            self.logger.info(
                "Recording enrollment sample %d/%d for user %s.",
                target_sample_number,
                self.samples_required,
                user_id,
            )
            audio = self.audio_recorder.record()
            quality_ok, quality_message = self.audio_recorder.is_quality_ok(audio)
            if not quality_ok:
                self.logger.warning(
                    "Rejected enrollment sample attempt %d for %s: %s",
                    attempts,
                    user_id,
                    quality_message,
                )
                continue

            embedding = self.speaker_encoder.encode(audio)
            embeddings.append(embedding)
            self.logger.info(
                "Accepted enrollment sample %d/%d for %s.",
                len(embeddings),
                self.samples_required,
                user_id,
            )

        if len(embeddings) < self.samples_required:
            raise RuntimeError(
                f"Unable to collect {self.samples_required} valid enrollment samples "
                f"within {self.max_attempts} attempts."
            )

        template = self._average_embeddings(embeddings)
        user_record = {
            "user_id": user_id.strip(),
            "name": name.strip(),
            "embedding": template.astype(float).tolist(),
            "role": effective_role,
            "permissions": effective_permissions,
            "enrolled_at": datetime.now(timezone.utc).isoformat(),
        }
        stored_record = self.user_store.add_user(user_record)
        self.logger.info("Enrollment completed for user %s.", user_id)
        return stored_record

    def _average_embeddings(self, embeddings: list[np.ndarray]) -> np.ndarray:
        """Average and normalize a list of speaker embeddings."""

        stacked_embeddings = np.stack(embeddings, axis=0).astype(np.float32, copy=False)
        mean_embedding = np.mean(stacked_embeddings, axis=0, dtype=np.float32)
        norm = float(np.linalg.norm(mean_embedding))
        if norm == 0.0:
            raise RuntimeError("Averaged speaker embedding has zero magnitude.")
        return mean_embedding / norm
