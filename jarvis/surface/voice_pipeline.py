"""L5 voice pipeline — composition site for wake + PTT paths (ADR-0005 §4.2).

Owns the wake/PTT mutex (``VOICE_INPUT_LOCK``) so the two input paths
cannot double-capture the default mic (ADR-0005 §8 fix #2). Calls
recognize -> empty-check -> optional artifact write ->
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
import time
from typing import TYPE_CHECKING, Protocol

from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.state.event_log import emit_event
from jarvis.surface import voice_artifact_store, voice_asr

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Mapping
    from pathlib import Path

    from jarvis.shared import Event
    from jarvis.surface.voice_cues import VoiceCues
    from jarvis.surface.voice_print import VoiceprintExtractor


LOGGER = logging.getLogger("jarvis.surface.voice_pipeline")

# Wake/PTT mutex. Module-global; both paths share the same lock instance.
VOICE_INPUT_LOCK = threading.Lock()


class VoicePipelineError(RuntimeError):
    """Base for voice-pipeline failures the caller may catch."""


class VoiceInputBusyError(VoicePipelineError):
    """``VOICE_INPUT_LOCK`` could not be acquired within the timeout."""


class VoicePipelineEmptyError(VoicePipelineError):
    """Transcript was empty / silent / punctuation-only after ASR.

    ``heard`` keeps what the recognizer returned, for an owner that acts on a
    lone word too short to be a turn (a single 「停」 while Jarvis talks).
    """

    def __init__(self, message: str, *, heard: str = "") -> None:
        """Keep the recognizer's text beside the message."""
        super().__init__(message)
        self.heard = heard


class VoicePipelineWakeOnlyError(VoicePipelineEmptyError):
    """A wake-channel transcript held only the wake phrase; the question is still to come."""


class VoicePipelineAbsorbedError(VoicePipelineEmptyError):
    """``before_emit`` took the words as no turn: a listening sound, or a stop it acted on."""

    def __init__(self, reason: str) -> None:
        """``reason`` rides the ``empty`` phase the surface gets instead of ``accepted``."""
        super().__init__(f"absorbed as {reason}")
        self.reason = reason


class UtteranceJudgedError(VoicePipelineError):
    """The brain judged the words as it received them and they are no turn (ADR 0277).

    Raised by an ``emit`` that sends the words with the owner's judge context; ``verdict`` is the
    word judge's (``dismissed``, ``backchannel``, ...), and nothing was written as a turn.
    """

    def __init__(self, verdict: str) -> None:
        """Keep the brain's ``verdict`` beside the message."""
        super().__init__(f"judged as {verdict}")
        self.verdict = verdict


class _BroadcasterProtocol(Protocol):
    """Subset of InherentBroadcaster that voice_pipeline calls."""

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None: ...


class VoicePipeline:
    """Run one voice turn from raw audio bytes to utterance.received emit.

    The pipeline does NOT capture audio itself — the wake listener and
    the ``/inherent/asr-submit/v2`` handler each capture (or receive) audio
    and call ``run_turn(...)`` for the ASR-and-emit phase.
    """

    def __init__(  # noqa: PLR0913 — 6 keyword-only deps form the L5 composition boundary.
        self,
        *,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
        recognizer: voice_asr.AsrRecognizer,
        broadcaster: _BroadcasterProtocol | None,
        artifacts_dir: Path | None,
        sample_rate_hz: int = 16000,
        cues: VoiceCues | None = None,
        emit: Callable[[Mapping[str, object], Mapping[str, str]], Event] | None = None,
        voiceprints: VoiceprintExtractor | None = None,
    ) -> None:
        """Wire together one VoicePipeline; see class docstring for semantics.

        The utterance is committed to the log ``conn_factory`` opens, or, on a voice terminal
        (ADR 0172), handed to ``emit(payload, correlation)``, which returns the committed
        event. Exactly one of the two is given. With ``voiceprints`` (ADR 0286) a turn that
        names its microphone is embedded beside its final ASR.
        """
        if (conn_factory is None) == (emit is None):
            msg = "a VoicePipeline commits to a log or hands the utterance on, not both or neither"
            raise ValueError(msg)
        self._emit = emit
        self._conn_factory = conn_factory
        self._recognizer = recognizer
        self._broadcaster = broadcaster
        self._artifacts_dir = artifacts_dir
        self._sample_rate_hz = sample_rate_hz
        self._cues = cues  # ADR 0152: the clip's tone and sounds, for her next turn
        self._voiceprints = voiceprints

    def prewarm_input_model(self) -> None:
        """Prewarm the concrete local ASR provider for single-ingress activation."""
        prewarm = getattr(self._recognizer, "prewarm", None)
        if not callable(prewarm):
            msg = "configured ASR recognizer does not expose prewarm()"
            raise TypeError(msg)
        prewarm()

    def warm_input_model(self) -> None:
        """Allen started talking: let a recognizer that cools when idle warm up now."""
        warm = getattr(self._recognizer, "warm", None)
        if callable(warm):
            warm()

    def partial_text(self, audio_bytes: bytes) -> str:
        """Decode one bounded snapshot for the L5 endpoint decision (ADR-0006 D7)."""
        partial = getattr(self._recognizer, "partial_text", None)
        if not callable(partial):
            msg = "configured ASR recognizer does not expose partial_text()"
            raise TypeError(msg)
        text: str = partial(audio_bytes)
        return text

    def prepare_final(self, utterance_id: str, audio_bytes: bytes, speech_s: float) -> None:
        """Allen went quiet: a recognizer that can hear ahead of the commit starts now.

        Only a hint, handed over and never waited for; ``run_turn`` for the same
        ``utterance_id`` collects the result. A recognizer without ``prepare`` ignores it.
        """
        prepare = getattr(self._recognizer, "prepare", None)
        if callable(prepare):
            prepare(utterance_id, audio_bytes, speech_s)

    def on_prepared_text(self, listener: Callable[[str, int, str], None]) -> bool:
        """Hand ``listener`` the text of each pass the recognizer prepared (ADR 0145).

        Returns whether the recognizer prepares at all; one that does not never calls it.
        The listener runs on the recognizer's worker thread.
        """
        register = getattr(self._recognizer, "on_prepared_text", None)
        if not callable(register):
            return False
        register(listener)
        return True

    def discard_final(self, utterance_id: str) -> None:
        """He spoke again, or the utterance never committed: drop what was prepared for it."""
        discard = getattr(self._recognizer, "discard", None)
        if callable(discard):
            discard(utterance_id)

    def transcribe(
        self,
        audio_bytes: bytes,
        *,
        recognizer: voice_asr.AsrRecognizer | None = None,
        rehear_among: Collection[str] = (),
    ) -> voice_asr.DictationHeard:
        """Dictation (ADR 0076/0077): one stretch between his pauses, heard and corrected, no emit.

        ``recognizer`` hears it instead of the voice path's own, as 言字 does
        (local Whisper, ADR 0110); ``rehear_among`` lets it hear a short clip again (ADR 0175).
        No words when nothing in it is speech; the caller judges the joined stretches as a whole.
        """
        if recognizer is not None:
            return voice_asr.dictation_text(audio_bytes, recognizer, rehear_among=rehear_among)
        if voice_asr.too_quiet_for_speech(audio_bytes):
            return voice_asr.DictationHeard("", "")
        return voice_asr.DictationHeard(self.partial_text(audio_bytes), "")

    def _judge[T](
        self,
        ask: Callable[[], T],
        *,
        turn_id: str,
        broadcast: bool,
    ) -> T:
        """Hand the owner the words; an utterance it absorbs ends as ``empty`` on the wire."""
        try:
            return ask()
        except VoicePipelineAbsorbedError as absorbed:
            if broadcast and self._broadcaster is not None:
                self._broadcaster.broadcast_voice_sync(
                    "empty", turn_id=turn_id, reason=absorbed.reason,
                )
            raise

    def run_turn(  # noqa: C901, PLR0912, PLR0913, PLR0915 — wake/PTT toggles widen the signature; splitting would shred the single locked critical section.
        self,
        *,
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
        lock_acquire_timeout_s: float = 2.0,
        broadcast: bool = True,
        session_id: str | None = None,
        utterance_id: str | None = None,
        endpoint_reason: str | None = None,
        before_emit: Callable[[str, Mapping[str, object] | None], Mapping[str, object] | None]
        | None = None,
        on_judged: Callable[[str, str], None] | None = None,
        wake_lead: bool = True,
        speech_s: float = 0.0,
        spoke_until: float = 0.0,
        mic: tuple[str, float] | None = None,
    ) -> Event:
        """Execute one voice turn end-to-end. Returns the emitted Event row.

        Args:
            audio_bytes: Captured PCM16 little-endian mono audio.
            turn_id: Server-minted turn id (``T<hex>``).
            channel: ``"inherent_wake"`` or ``"inherent_ptt"``.
            language: Spoken language hint (e.g. ``"zh-CN"``).
            lock_acquire_timeout_s: Timeout for the ``VOICE_INPUT_LOCK`` acquire.
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
            before_emit: Called with the transcript and the utterance's
                voiceprint (ADR 0286, ``None`` without one) once it is
                accepted, before ``utterance.received`` is written:
                conversation mode drops the unspoken answer to Allen's
                previous sentence there, so the new turn can never see it
                (ADR 0053), and raises :class:`VoicePipelineAbsorbedError`
                for words spoken over Jarvis that are no turn. What it returns,
                when it returns a mapping, travels with the utterance as ``judge``
                for a brain that judges the words on receipt (ADR 0277); ``emit``
                then sends it and raises :class:`UtteranceJudgedError` for words
                that are no turn. Without ``emit`` the mapping is added to the
                committed payload.
            on_judged: ``(words, verdict)`` once the brain has judged the
                words ``before_emit`` described, ``"turn"`` included. It carries
                out the verdict and raises :class:`VoicePipelineAbsorbedError`
                for words that are no turn, as ``before_emit`` does.
            wake_lead: The wake word opened this turn, or it was said over
                her, so a wake phrase is a lead to cut; ``False`` keeps a
                bare "Hey Jarvis" as a greeting.
            speech_s: Seconds of speech in ``audio_bytes``. Used only by a
                recognizer that was handed a ``prepare_final`` for the
                supplied ``utterance_id`` and has nothing usable of it.
            spoke_until: Monotonic time of the utterance's last speech frame (ADR 0272);
                ``0.0`` when unknown. The committed utterance says how long ago it was.
            mic: ``(name, seconds since its stream opened)`` of the microphone that heard
                the utterance (ADR 0286); with it and ``voiceprints`` the audio is embedded
                beside the final ASR and a terminal's utterance carries the ``voiceprint``.

        Raises:
            VoiceInputBusyError: VOICE_INPUT_LOCK contention (PTT path: 503).
            VoicePipelineEmptyError: transcript empty / too short / silent.
            VoicePipelineAbsorbedError: ``before_emit`` absorbed the words
                (a subclass, announced to the surface as ``empty``).
            VoicePipelineWakeOnlyError: a wake transcript held only the wake
                phrase (a subclass, so an owner that cannot re-listen treats
                it as empty).
            Exception: any unexpected ASR failure (caller decides reaction).
        """
        prepared = utterance_id is not None
        utterance_id = utterance_id or "U" + secrets.token_hex(8)
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
            embedding = None if self._voiceprints is None or mic is None else (
                self._voiceprints.begin(
                    audio_bytes, self._sample_rate_hz,
                    seconds=speech_s or len(audio_bytes) / 2 / self._sample_rate_hz,
                    mic=mic[0], mic_age_s=mic[1],
                )
            )
            recognize_prepared = getattr(self._recognizer, "recognize_prepared", None)
            if prepared and callable(recognize_prepared):
                tr = recognize_prepared(utterance_id, audio_bytes, speech_s)
            else:
                tr = self._recognizer.recognize(audio_bytes)
            if self._cues is not None:
                self._cues.heard(tr.text, tr.emotion, tr.event)
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
                raise VoicePipelineEmptyError(msg, heard=tr.text)
            said = voice_asr.strip_wake_lead(tr.text) if channel == "inherent_wake" else tr.text
            if channel == "inherent_wake" and not wake_lead and (
                voice_asr.is_wake_only(tr.text) or not said.strip()
            ):
                # A turn he opened himself (a tap, conversation mode): a bare
                # "Hey Jarvis" is a greeting to answer, not a wake to wait past.
                # A lead before more words is still cut, so Tier 0 matches.
                said = tr.text
            elif channel == "inherent_wake" and (
                voice_asr.is_wake_only(tr.text) or not said.strip()
            ):
                # Not a question: the wake owner keeps listening for the next
                # utterance and says so on the wire itself.
                msg = f"wake phrase only for turn_id={turn_id}: {tr.text!r}"
                raise VoicePipelineWakeOnlyError(msg, heard=tr.text)

            voiceprint = None if embedding is None else embedding()
            judge: Mapping[str, object] | None = None
            if before_emit is not None:
                judge = self._judge(
                    lambda: before_emit(said, voiceprint), turn_id=turn_id,
                    broadcast=broadcast,
                )

            # 3. Audio retention for memory.db (None = off): the utterance names the file,
            # which is written after it is sent (ADR 0277).
            artifact_ref = voice_artifact_store.planned_ref(turn_id, self._artifacts_dir)

            # 4. Emit utterance.received via fresh connection (worker thread).
            payload: dict[str, object] = {
                "transcript": said,
                "turn_id": turn_id,
                "channel": channel,
                "language": language,
                "confidence": tr.confidence,
            }
            if tr.language_detected:
                payload["language_detected"] = tr.language_detected
            if tr.emotion:
                payload["emotion"] = tr.emotion
            # ADR 0270: `confidence` is the writing engine's own score; these say whose it is.
            if tr.asr_engine:
                payload["asr_engine"] = tr.asr_engine
            if tr.sensevoice_confidence is not None:
                payload["sensevoice_confidence"] = tr.sensevoice_confidence
            if tr.whisper_confidence is not None:
                payload["whisper_confidence"] = tr.whisper_confidence
            if artifact_ref:
                payload["audio_artifact_ref"] = artifact_ref
            if session_id is not None:
                payload["session_id"] = session_id
            payload["utterance_id"] = utterance_id
            if endpoint_reason is not None:
                payload["endpoint_reason"] = endpoint_reason
            if spoke_until > 0:
                # ADR 0272: how long ago he stopped, for the hold his answer waits out.
                payload["spoke_ago_ms"] = max(0, round((time.monotonic() - spoke_until) * 1000))

            if self._emit is None:
                payload.update(judge or {})  # the owner's own additions to the row
            else:
                if judge is not None:
                    payload["judge"] = dict(judge)
                if voiceprint is not None:
                    payload["voiceprint"] = voiceprint  # the brain scores it (ADR 0286)

            if self._emit is not None:  # a voice terminal sends it to the brain (ADR 0172)
                try:
                    ev = self._emit(payload, {"turn_id": turn_id})
                except UtteranceJudgedError as judged:
                    self._judged(on_judged, said, judged.verdict, turn_id, broadcast)
                    raise  # unreachable: _judged absorbs every verdict but "turn"
                self._judged(on_judged, said, "turn", turn_id, broadcast)
            else:
                assert self._conn_factory is not None  # noqa: S101 — the constructor ensures it.
                with contextlib.closing(self._conn_factory()) as worker_conn:
                    ev = emit_event(
                        worker_conn,
                        type="utterance.received",
                        payload=payload,
                        correlation={"turn_id": turn_id},
                    )
            voice_artifact_store.persist_later(
                audio_bytes,
                turn_id=turn_id,
                sample_rate_hz=self._sample_rate_hz,
                artifacts_dir=self._artifacts_dir,
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
            # 5. Wake-path UI notify (PTT path passes broadcast=False so
            # Swift drives the card from the HTTP response, not WS).
            # Wire field is "text" to match the PTT HTTP contract
            # (inherent_server.py maps transcript -> text) and the Swift
            # voiceState handler which reads payload["text"]. Without
            # this rename the Swift card sees text=nil and falls into
            # the "no speech" branch even when ASR transcribed cleanly.
            if broadcast and self._broadcaster is not None:
                accepted_payload: dict[str, object] = {"text": said}
                if tr.emotion:
                    accepted_payload["emotion"] = tr.emotion
                self._broadcaster.broadcast_voice_sync(
                    "accepted", turn_id=turn_id, **accepted_payload,
                )
            return ev
        finally:
            VOICE_INPUT_LOCK.release()

    def _judged(
        self,
        on_judged: Callable[[str, str], None] | None,
        words: str,
        verdict: str,
        turn_id: str,
        broadcast: bool,  # noqa: FBT001 - run_turn's own flag
    ) -> None:
        """Carry out the brain's verdict; every one but ``turn`` ends the turn as absorbed."""

        def act(text: str) -> None:
            if on_judged is not None:
                on_judged(text, verdict)
            if verdict != "turn":
                raise VoicePipelineAbsorbedError(verdict)

        self._judge(lambda: act(words), turn_id=turn_id, broadcast=broadcast)


__all__ = [
    "VOICE_INPUT_LOCK",
    "UtteranceJudgedError",
    "VoiceInputBusyError",
    "VoicePipeline",
    "VoicePipelineAbsorbedError",
    "VoicePipelineEmptyError",
    "VoicePipelineError",
    "VoicePipelineWakeOnlyError",
]
