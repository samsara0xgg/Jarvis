"""L5 voice pipeline — composition site for wake + PTT paths (ADR-0005 §4.2).

Owns the wake/PTT mutex (``VOICE_INPUT_LOCK``) so the two input paths
cannot double-capture the default mic (ADR-0005 §8 fix #2). Calls
recognize -> empty-check -> normalize -> optional artifact write ->
``emit_event("utterance.received", ...)`` per spec §3.6.1.

Layer rules: imports only stdlib, ``jarvis.shared``, ``jarvis.state.event_log``,
and sibling ``jarvis.surface.voice_*`` modules. Does NOT name
``jarvis.decision``, ``jarvis.execution``, ``jarvis.deployment``,
``jarvis.runtime``, or ``jarvis.cli``.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Protocol

from jarvis.state.event_log import emit_event
from jarvis.surface import voice_artifact_store, voice_asr

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event


LOGGER = logging.getLogger("jarvis.surface.voice_pipeline")

# Wake/PTT mutex. Module-global; both paths share the same lock instance.
VOICE_INPUT_LOCK = threading.Lock()


class VoicePipelineError(RuntimeError):
    """Base for voice-pipeline failures the caller may catch."""


class VoiceInputBusyError(VoicePipelineError):
    """``VOICE_INPUT_LOCK`` could not be acquired within the timeout."""


class VoicePipelineEmptyError(VoicePipelineError):
    """Transcript was empty / silent / punctuation-only after ASR."""


class _BroadcasterProtocol(Protocol):
    """Subset of InherentBroadcaster that voice_pipeline calls."""

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None: ...


class VoicePipeline:
    """Run one voice turn from raw audio bytes to utterance.received emit.

    The pipeline does NOT capture audio itself — the wake listener and
    the ``/inherent/asr-submit`` handler each capture (or receive) audio
    and call ``run_turn(...)`` for the ASR-and-emit phase.
    """

    def __init__(  # noqa: PLR0913 — 6 keyword-only deps form the L5 composition boundary.
        self,
        *,
        conn_factory: Callable[[], sqlite3.Connection],
        recognizer: voice_asr.AsrRecognizer,
        normalizer: voice_asr.AsrNormalizer,
        broadcaster: _BroadcasterProtocol | None,
        artifacts_dir: Path,
        sample_rate_hz: int = 16000,
    ) -> None:
        """Wire together one VoicePipeline; see class docstring for semantics."""
        self._conn_factory = conn_factory
        self._recognizer = recognizer
        self._normalizer = normalizer
        self._broadcaster = broadcaster
        self._artifacts_dir = artifacts_dir
        self._sample_rate_hz = sample_rate_hz

    def run_turn(
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_acquire_timeout_s: float = 2.0,
    ) -> Event:
        """Execute one voice turn end-to-end. Returns the emitted Event row.

        Raises:
            VoiceInputBusyError: VOICE_INPUT_LOCK contention (PTT path: 503).
            VoicePipelineEmptyError: transcript empty / too short / silent.
            Exception: any unexpected ASR failure (caller decides reaction).
        """
        acquired = VOICE_INPUT_LOCK.acquire(timeout=lock_acquire_timeout_s)
        if not acquired:
            msg = (
                f"VOICE_INPUT_LOCK busy after {lock_acquire_timeout_s}s; "
                f"turn_id={turn_id}"
            )
            raise VoiceInputBusyError(msg)
        try:
            # 1. Recognize (sync ASR call).
            tr = self._recognizer.recognize(audio_bytes)

            # 2. Empty / too-short filter — ADR §8 fix #3 (unified).
            if voice_asr.is_empty_or_too_short(tr.text, audio_pcm=audio_bytes):
                if self._broadcaster is not None:
                    self._broadcaster.broadcast_voice_sync(
                        "empty", turn_id=turn_id, reason="no_speech",
                    )
                msg = f"empty utterance for turn_id={turn_id}"
                raise VoicePipelineEmptyError(msg)

            # 3. Normalize BEFORE emit — ADR §8 fix #1 (spec §3.6.2).
            normalized = self._normalizer.normalize(tr.text)

            # 4. Optional raw-WAV artifact retention.
            artifact_ref = voice_artifact_store.persist(
                audio_bytes,
                turn_id=turn_id,
                sample_rate_hz=self._sample_rate_hz,
                artifacts_dir=self._artifacts_dir,
            )

            # 5. Emit utterance.received via fresh connection (worker thread).
            payload: dict[str, object] = {
                "transcript": normalized,
                "turn_id": turn_id,
                "channel": channel,
                "language": language,
                "confidence": tr.confidence,
            }
            if tr.language_detected:
                payload["language_detected"] = tr.language_detected
            if tr.emotion:
                payload["emotion"] = tr.emotion
            if artifact_ref:
                payload["audio_artifact_ref"] = artifact_ref

            with contextlib.closing(self._conn_factory()) as worker_conn:
                ev = emit_event(
                    worker_conn,
                    type="utterance.received",
                    payload=payload,
                    correlation={"turn_id": turn_id},
                )

            # 6. Wake-path UI notify (PTT broadcaster is None — caller
            # handles UI via HTTP response).
            if self._broadcaster is not None:
                accepted_payload: dict[str, object] = {"transcript": normalized}
                if tr.emotion:
                    accepted_payload["emotion"] = tr.emotion
                self._broadcaster.broadcast_voice_sync(
                    "accepted", turn_id=turn_id, **accepted_payload,
                )
            return ev
        finally:
            VOICE_INPUT_LOCK.release()


__all__ = [
    "VOICE_INPUT_LOCK",
    "VoiceInputBusyError",
    "VoicePipeline",
    "VoicePipelineEmptyError",
    "VoicePipelineError",
]
