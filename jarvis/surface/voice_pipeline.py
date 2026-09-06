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
import secrets
import threading
from typing import TYPE_CHECKING, Protocol

from jarvis.shared.realtime_trace import record_realtime_trace
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

    def prewarm_input_model(self) -> None:
        """Prewarm the concrete local ASR provider for single-ingress activation."""
        prewarm = getattr(self._recognizer, "prewarm", None)
        if not callable(prewarm):
            msg = "configured ASR recognizer does not expose prewarm()"
            raise TypeError(msg)
        prewarm()

    def partial_text(self, audio_bytes: bytes) -> str:
        """Decode one bounded snapshot for the L5 endpoint decision (ADR-0006 D7)."""
        partial = getattr(self._recognizer, "partial_text", None)
        if not callable(partial):
            msg = "configured ASR recognizer does not expose partial_text()"
            raise TypeError(msg)
        text: str = partial(audio_bytes)
        return text

    def run_turn(  # noqa: C901, PLR0912, PLR0913 — wake/PTT toggles widen the signature; splitting would shred the single locked critical section.
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_acquire_timeout_s: float = 2.0,
        lock_already_held: bool = False,
        broadcast: bool = True,
        session_id: str | None = None,
        utterance_id: str | None = None,
        endpoint_reason: str | None = None,
    ) -> Event:
        """Execute one voice turn end-to-end. Returns the emitted Event row.

        Args:
            audio_bytes: Captured PCM16 little-endian mono audio.
            turn_id: Server-minted turn id (``T<hex>``).
            channel: ``"inherent_wake"`` or ``"inherent_ptt"``.
            language: Spoken language hint (e.g. ``"zh-CN"``).
            lock_acquire_timeout_s: Timeout for the internal
                ``VOICE_INPUT_LOCK`` acquire when ``lock_already_held``
                is False. Ignored when the caller already owns the lock.
            lock_already_held: When True, the caller already holds
                :data:`VOICE_INPUT_LOCK` for the full capture+ASR turn
                (wake listener path — ADR-0005 §8 fix #2). The pipeline
                must NOT try to acquire / release it (``threading.Lock``
                is non-reentrant — same-thread re-acquire would deadlock).
            broadcast: When False, suppress the ``voice("accepted", ...)``
                and ``voice("empty", ...)`` phase envelopes. PTT path
                passes False (ADR-0005 §6 — Swift drives the card from
                the HTTP response, not WS envelopes); wake path passes
                True (default) so the WS-driven card sees the phase
                transitions.
            session_id: Optional realtime voice-session identity added to the
                committed utterance payload.
            utterance_id: Realtime utterance identity added to the committed
                utterance payload. The wake path supplies its own (one per
                endpointed utterance); the PTT path does not, so an unsupplied
                id is minted here — one press-to-release is one utterance.
            endpoint_reason: Optional typed acoustic endpoint reason.

        Raises:
            VoiceInputBusyError: VOICE_INPUT_LOCK contention (PTT path: 503).
                Only raised when ``lock_already_held`` is False.
            VoicePipelineEmptyError: transcript empty / too short / silent.
            Exception: any unexpected ASR failure (caller decides reaction).
        """
        utterance_id = utterance_id or "U" + secrets.token_hex(8)
        if not lock_already_held:
            acquired = VOICE_INPUT_LOCK.acquire(timeout=lock_acquire_timeout_s)
            if not acquired:
                msg = (
                    f"VOICE_INPUT_LOCK busy after {lock_acquire_timeout_s}s; "
                    f"turn_id={turn_id}"
                )
                raise VoiceInputBusyError(msg)
        try:
            # 1. Recognize (sync ASR call). The caller's endpoint/audio
            # assembly is a distinct software milestone; this point is the
            # authoritative SenseVoice result, not ADC or acoustic truth.
            record_realtime_trace(
                "asr_final_started",
                turn_id=turn_id,
                channel=channel,
                audio_bytes=len(audio_bytes),
                measurement_boundary="authoritative_asr_call_started",
            )
            tr = self._recognizer.recognize(audio_bytes)
            record_realtime_trace(
                "asr_final",
                turn_id=turn_id,
                channel=channel,
                transcript_characters=len(tr.text),
                measurement_boundary="authoritative_sensevoice_result",
            )

            # 2. Empty / too-short filter — ADR §8 fix #3 (unified).
            if voice_asr.is_empty_or_too_short(tr.text, audio_pcm=audio_bytes):
                if broadcast and self._broadcaster is not None:
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
            if session_id is not None:
                payload["session_id"] = session_id
            payload["utterance_id"] = utterance_id
            if endpoint_reason is not None:
                payload["endpoint_reason"] = endpoint_reason

            with contextlib.closing(self._conn_factory()) as worker_conn:
                ev = emit_event(
                    worker_conn,
                    type="utterance.received",
                    payload=payload,
                    correlation={"turn_id": turn_id},
                )
            record_realtime_trace(
                "utterance_committed",
                turn_id=turn_id,
                channel=channel,
                session_id=session_id,
                utterance_id=utterance_id,
                endpoint_reason=endpoint_reason,
                event_uid=ev.event_uid,
                measurement_boundary="durable_utterance_received_commit",
            )
            # 6. Wake-path UI notify (PTT path passes broadcast=False so
            # Swift drives the card from the HTTP response, not WS).
            # Wire field is "text" to match the PTT HTTP contract
            # (inherent_server.py maps transcript -> text) and the Swift
            # voiceState handler which reads payload["text"]. Without
            # this rename the Swift card sees text=nil and falls into
            # the "no speech" branch even when ASR transcribed cleanly.
            if broadcast and self._broadcaster is not None:
                accepted_payload: dict[str, object] = {"text": normalized}
                if tr.emotion:
                    accepted_payload["emotion"] = tr.emotion
                self._broadcaster.broadcast_voice_sync(
                    "accepted", turn_id=turn_id, **accepted_payload,
                )
            return ev
        finally:
            if not lock_already_held:
                VOICE_INPUT_LOCK.release()


__all__ = [
    "VOICE_INPUT_LOCK",
    "VoiceInputBusyError",
    "VoicePipeline",
    "VoicePipelineEmptyError",
    "VoicePipelineError",
]
