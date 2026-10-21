"""What the media owner answers its callers with, and the provider it is given."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from jarvis.shared import lang
    from jarvis.surface.voice_tts import TTSSession

MediaSubmitStatus = Literal[
    "accepted",
    "duplicate",
    "historical",
    "unregistered",
    "terminal",
    "closed",
    "overloaded",
    "invalid",
]


@dataclass(frozen=True)
class MediaSubmitOutcome:
    """Bounded watcher/direct-delivery disposition."""

    status: MediaSubmitStatus
    event_uid: str
    response_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class MediaPowerTransitionResult:
    """Bounded power-lifecycle result for the persistent output owner."""

    status: Literal["suspended", "resumed", "uncertain", "closed"]
    attempt_id: int
    reason: str
    helper_thread_alive: bool = False
    deadline_exhausted: bool = False

    @property
    def succeeded(self) -> bool:
        """Return whether the requested output state is proven."""
        return self.status in {"suspended", "resumed", "closed"}


class StreamingMediaStartupError(RuntimeError):
    """Bounded startup failure with an explicit audio-device isolation debt."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        device_disposition: Literal["not_attempted", "failed_closed", "uncertain"],
    ) -> None:
        """Record the phase and exact typed output-ownership disposition."""
        super().__init__(message)
        self.phase = phase
        self.device_disposition = device_disposition
        self.device_state_uncertain = device_disposition == "uncertain"


class StreamingTTSProvider(Protocol):
    """Provider factory consumed only by the persistent media loop."""

    @property
    def streaming_candidate_count(self) -> int:
        """Return ordered prefix-safe endpoint candidates."""

    def create_tts_session(
        self,
        *,
        endpoint_index: int,
        language: lang.Language,
        idle_close_s: float,
        command_queue_capacity: int,
        audio_queue_capacity: int,
    ) -> TTSSession:
        """Create a closed response-scoped session that reads ``language``."""
