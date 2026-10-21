"""The media owner's bounds: every queue and wait is explicitly bounded."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_MAX_MEDIA_COMMANDS = 1024
_MAX_AFTER_DRAIN_RESPONSES = 64
_MAX_RESPONSE_TEXT_BYTES = 1_048_576
_MAX_SESSION_COMMANDS = 32
_MAX_SESSION_AUDIO_EVENTS = 256
_MAX_RESPONSE_TIMEOUT_S = 300.0
_MAX_SHUTDOWN_TIMEOUT_S = 10.0
_MAX_EVENT_DRAIN_BATCH = 1024
_MAX_DURABILITY_RETRY_ATTEMPTS = 10


@dataclass(frozen=True)
class StreamingMediaConfig:
    """All queues and network/presentation waits are explicitly bounded."""

    canonical_sample_rate_hz: int = 48_000
    command_queue_capacity: int = 64
    response_lane_capacity: int = 8
    response_text_bytes: int = 256 * 1024
    session_command_capacity: int = 2
    session_audio_capacity: int = 16
    session_idle_close_s: float = 10.0
    response_timeout_s: float = 45.0
    ring_retry_s: float = 0.002
    # Seconds of PCM the streaming player's ring holds. Small on purpose: the
    # streaming path writes as chunks arrive, so headroom buys nothing and
    # costs interrupt latency, unlike the legacy player's whole-buffer write.
    ring_seconds: float = 2.0
    presentation_poll_s: float = 0.005
    shutdown_timeout_s: float = 3.0
    event_drain_batch: int = 128
    terminal_retry_attempts: int = 3
    checkpoint_retry_attempts: int = 3
    durability_retry_s: float = 0.02
    enable_macos_say_fallback: bool = True
    speak_from_segments: bool = False
    prefetch_network_lost_line: bool = True
    # Where the line's audio is kept between runs; ``None`` keeps it in memory only. A voice
    # terminal sets it: the audio of a fixed line is not the owner's state (ADR 0172).
    network_lost_cache_dir: Path | None = None
    # ADR 0165: drop the provider's silence at generation start and junctions.
    trim_silence: bool = False
    trim_threshold_db: float = -50.0
    trim_window_ms: float = 5.0
    trim_preroll_ms: float = 20.0
    trim_fade_ms: float = 5.0
    trim_max_cut_ms: float = 400.0
    trim_clause_pause_ms: float = 120.0
    trim_sentence_pause_ms: float = 250.0

    def __post_init__(self) -> None:
        """Reject unbounded or non-positive actor budgets."""
        positive_ints = (
            self.canonical_sample_rate_hz,
            self.command_queue_capacity,
            self.response_lane_capacity,
            self.response_text_bytes,
            self.session_command_capacity,
            self.session_audio_capacity,
            self.event_drain_batch,
            self.terminal_retry_attempts,
            self.checkpoint_retry_attempts,
        )
        positive_floats = (
            self.session_idle_close_s,
            self.response_timeout_s,
            self.ring_retry_s,
            self.ring_seconds,
            self.presentation_poll_s,
            self.shutdown_timeout_s,
            self.durability_retry_s,
            self.trim_window_ms,
            self.trim_preroll_ms,
            self.trim_fade_ms,
            self.trim_max_cut_ms,
            self.trim_clause_pause_ms,
            self.trim_sentence_pause_ms,
        )
        if self.trim_threshold_db >= 0:
            msg = "trim_threshold_db must be below 0 dBFS"
            raise ValueError(msg)
        if any(value <= 0 for value in positive_ints + positive_floats):
            msg = "streaming media bounds must all be positive"
            raise ValueError(msg)
        if (
            self.command_queue_capacity > _MAX_MEDIA_COMMANDS
            or self.response_lane_capacity > _MAX_AFTER_DRAIN_RESPONSES
            or self.response_text_bytes > _MAX_RESPONSE_TEXT_BYTES
            or self.session_command_capacity > _MAX_SESSION_COMMANDS
            or self.session_audio_capacity > _MAX_SESSION_AUDIO_EVENTS
            or self.response_timeout_s > _MAX_RESPONSE_TIMEOUT_S
            or self.shutdown_timeout_s > _MAX_SHUTDOWN_TIMEOUT_S
            or self.event_drain_batch > _MAX_EVENT_DRAIN_BATCH
            or self.terminal_retry_attempts > _MAX_DURABILITY_RETRY_ATTEMPTS
            or self.checkpoint_retry_attempts > _MAX_DURABILITY_RETRY_ATTEMPTS
        ):
            msg = "streaming media bounds exceed validated production maxima"
            raise ValueError(msg)


def streaming_media_config_from_mapping(  # noqa: C901 - one typed reader per key kind
    raw: Mapping[str, object] | None,
) -> StreamingMediaConfig:
    """Parse bounded rollout values; reject malformed explicit entries."""
    values = {} if raw is None else raw
    defaults = StreamingMediaConfig()

    def _positive_int(key: str, fallback: int) -> int:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            msg = f"realtime.streaming_output.{key} must be a positive integer"
            raise ValueError(msg)
        return value

    def _positive_float(key: str, fallback: float) -> float:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            msg = f"realtime.streaming_output.{key} must be a positive number"
            raise ValueError(msg)
        return float(value)

    def _negative_float(key: str, fallback: float) -> float:
        value = values.get(key)
        if value is None:
            return fallback
        if isinstance(value, bool) or not isinstance(value, int | float) or value >= 0:
            msg = f"realtime.streaming_output.{key} must be a negative number"
            raise ValueError(msg)
        return float(value)

    def _boolean(key: str, fallback: bool) -> bool:  # noqa: FBT001 - config default
        value = values.get(key)
        if value is None:
            return fallback
        if not isinstance(value, bool):
            msg = f"realtime.streaming_output.{key} must be boolean"
            raise ValueError(msg)  # noqa: TRY004 - malformed config value, not a caller type
        return value

    return StreamingMediaConfig(
        canonical_sample_rate_hz=_positive_int(
            "canonical_sample_rate_hz",
            defaults.canonical_sample_rate_hz,
        ),
        command_queue_capacity=_positive_int(
            "command_queue_capacity",
            defaults.command_queue_capacity,
        ),
        response_lane_capacity=_positive_int(
            "response_lane_capacity",
            defaults.response_lane_capacity,
        ),
        response_text_bytes=_positive_int(
            "response_text_bytes",
            defaults.response_text_bytes,
        ),
        session_command_capacity=_positive_int(
            "session_command_capacity",
            defaults.session_command_capacity,
        ),
        session_audio_capacity=_positive_int(
            "session_audio_capacity",
            defaults.session_audio_capacity,
        ),
        session_idle_close_s=_positive_float(
            "session_idle_close_s",
            defaults.session_idle_close_s,
        ),
        response_timeout_s=_positive_float(
            "response_timeout_s",
            defaults.response_timeout_s,
        ),
        ring_retry_s=_positive_float("ring_retry_s", defaults.ring_retry_s),
        ring_seconds=_positive_float("ring_seconds", defaults.ring_seconds),
        presentation_poll_s=_positive_float(
            "presentation_poll_s",
            defaults.presentation_poll_s,
        ),
        shutdown_timeout_s=_positive_float(
            "shutdown_timeout_s",
            defaults.shutdown_timeout_s,
        ),
        event_drain_batch=_positive_int(
            "event_drain_batch",
            defaults.event_drain_batch,
        ),
        terminal_retry_attempts=_positive_int(
            "terminal_retry_attempts",
            defaults.terminal_retry_attempts,
        ),
        checkpoint_retry_attempts=_positive_int(
            "checkpoint_retry_attempts",
            defaults.checkpoint_retry_attempts,
        ),
        durability_retry_s=_positive_float(
            "durability_retry_s",
            defaults.durability_retry_s,
        ),
        enable_macos_say_fallback=_boolean(
            "enable_macos_say_fallback",
            defaults.enable_macos_say_fallback,
        ),
        speak_from_segments=_boolean("speak_from_segments", defaults.speak_from_segments),
        prefetch_network_lost_line=_boolean(
            "prefetch_network_lost_line", defaults.prefetch_network_lost_line,
        ),
        trim_silence=_boolean("trim_silence", defaults.trim_silence),
        trim_threshold_db=_negative_float("trim_threshold_db", defaults.trim_threshold_db),
        trim_window_ms=_positive_float("trim_window_ms", defaults.trim_window_ms),
        trim_preroll_ms=_positive_float("trim_preroll_ms", defaults.trim_preroll_ms),
        trim_fade_ms=_positive_float("trim_fade_ms", defaults.trim_fade_ms),
        trim_max_cut_ms=_positive_float("trim_max_cut_ms", defaults.trim_max_cut_ms),
        trim_clause_pause_ms=_positive_float(
            "trim_clause_pause_ms", defaults.trim_clause_pause_ms,
        ),
        trim_sentence_pause_ms=_positive_float(
            "trim_sentence_pause_ms", defaults.trim_sentence_pause_ms,
        ),
    )
