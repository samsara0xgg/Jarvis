"""Starting the capture session: the single audio ingress, or text and PTT upload only."""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, cast

from jarvis.runtime.daemon.devices import output_or_default
from jarvis.runtime.daemon.listen_ports import local_listen_ports
from jarvis.runtime.daemon.voice_power import SessionAttempt, VoiceInputOwners
from jarvis.runtime.daemon.voice_wiring import (
    ListeningHost,
    ListenPorts,
    VoiceKnobs,
    single_ingress_activation,
)
from jarvis.shared.realtime_trace import record_realtime_trace
from jarvis.surface import (
    voice_aec,
    voice_audio,
    voice_backend,
    voice_pipeline,
    voice_session,
    voice_wake,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.voice_media.pipeline import StreamingTTSPipeline

LOGGER = logging.getLogger(__name__)


def spawn_single_ingress_session(  # noqa: C901, PLR0911, PLR0913, PLR0915 - each pre/post-device downgrade has distinct ownership semantics
    *,
    runtime: ListeningHost,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: StreamingTTSPipeline | None,
    voice: VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
    conversation: Callable[[], bool] | None = None,
    set_conversation: Callable[[bool, str], None] | None = None,
    set_quiet: Callable[[str], None] | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
    ports: ListenPorts | None = None,
) -> SessionAttempt:
    """Start Wave 3, or say what came of the attempt.

    ``attempted=True`` means a device open was tried, even if startup failed: a
    timed-out foreign PortAudio open may still own the default microphone, unless
    ``definitively_closed`` says the close confirmed it free.
    ``attempted=False`` means no input owner was touched, so a failed
    prerequisite/config validation leaves the boot with text and PTT upload only.
    ``why`` names the cause for the caller's log.

    ``ports`` is how the session reaches the turn: by default direct calls into ``runtime``,
    which must then be a daemon's :class:`JarvisRuntime`; a voice terminal (ADR 0172) passes
    its calls over the brain link.
    """
    knobs = VoiceKnobs() if voice is None else voice
    activation = single_ingress_activation(runtime, tts=tts)
    if not activation.requested:
        return SessionAttempt(None, why=activation.reason)
    if not activation.capable:
        LOGGER.warning(
            "realtime.single_audio_ingress validation failed (%s); "
            "text and PTT upload only, no input owner opened.",
            activation.reason,
        )
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason=activation.reason,
            fallback="text_ptt_upload",
            input_owner_attempted=False,
        )
        return SessionAttempt(None, why=activation.reason)
    ingress_config = activation.ingress_config
    session_config = activation.session_config
    if ingress_config is None or session_config is None:
        msg = "validated single ingress activation lacks parsed config"
        raise RuntimeError(msg)
    engine = voice_wake.MicroWakeWordEngine()
    try:
        # Model construction/download happens before PortAudio owns the mic.
        engine.start()
    except Exception as exc:
        LOGGER.exception(
            "realtime.single_audio_ingress wake model failed before device open; "
            "text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            engine.close()
        return SessionAttempt(None, why=f"the wake model did not start: {exc!r}")
    try:
        # Wave 3 requires final-ASR model/stream readiness before the sole
        # device open. Feature-off and validation downgrade never execute it.
        pipeline.prewarm_input_model()
    except Exception as exc:
        LOGGER.exception(
            "realtime.single_audio_ingress SenseVoice prewarm failed before "
            "device open; text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            engine.close()
        return SessionAttempt(None, why=f"the recognizer did not warm up: {exc!r}")

    def _capability_changed(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        log = LOGGER.info if snapshot.local_capture_available else LOGGER.warning
        log(
            "audio input capability state=%s epoch=%s reason=%s "
            "wake=%s local_capture=%s ptt_upload=%s text=%s",
            snapshot.state.value,
            snapshot.stream_epoch,
            snapshot.reason,
            snapshot.wake_available,
            snapshot.local_capture_available,
            snapshot.ptt_upload_available,
            snapshot.text_available,
        )
        broadcaster.broadcast_voice_capability_sync(
            version=snapshot.version,
            state=snapshot.state.value,
            stream_epoch=snapshot.stream_epoch,
            reason=snapshot.reason,
            wake_available=snapshot.wake_available,
            local_capture_available=snapshot.local_capture_available,
            ptt_upload_available=snapshot.ptt_upload_available,
            text_available=snapshot.text_available,
            route_kind=snapshot.route_kind,
            allowed_barge_mode=snapshot.allowed_barge_mode,
        )

    ingress: voice_audio.AudioIngress | None = None
    try:
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=voice_backend.AudioInputFormat(
                sample_rate_hz=ingress_config.canonical_sample_rate_hz,
                # ADR 0103: open up to the wake channel; a mono device opens mono.
                channels=(ingress_config.wake_input_channel or 0) + 1,
                callback_frame_samples=ingress_config.canonical_frame_samples,
            ),
            open_timeout_s=ingress_config.backend_open_timeout_s,
            close_timeout_s=ingress_config.backend_close_timeout_s,
            # ADR 0052: the Settings page's microphone; null = the system default.
            device=(runtime.config.raw.get("realtime") or {}).get("input_device"),
        )
        ingress = voice_audio.AudioIngress(
            backend=backend,
            config=ingress_config,
            capability_sink=_capability_changed,
            echo_canceller=echo_canceller,
        )
        ingress.set_output_device(
            output_or_default((runtime.config.raw.get("realtime") or {}).get("output_device")),
        )
        vad = voice_audio.SileroVad(
            mode="record",
            model_path=silero_path,
            profiles=knobs.vad_profiles,
        )
        listen = (
            local_listen_ports(cast("JarvisRuntime", runtime), tts)
            if ports is None
            else ports
        )

        def _stop_speaking() -> None:
            # ADR 0041: Allen talked over Jarvis in conversation mode. Stop
            # what is audible now, then cancel an answer still being written
            # so it cannot start speaking over his new question.
            playback = (
                tts.stop_foreground_output(None, reason="barge_in")
                if tts is not None
                else "no_streaming_player"
            )
            generation = (
                listen.interrupt("conversation_speech") if listen.interrupt is not None
                else "no_open_run"
            )
            LOGGER.info(
                "conversation barge-in: playback=%s generation=%s", playback, generation,
            )
            if echo_canceller is not None:
                # One second on, so the recording also holds what followed the onset.
                threading.Timer(1.0, _dump_echo_history, args=(echo_canceller,)).start()

        def _hold_output(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
            # ADR 0053: while Allen's words are coming in, no run completes and
            # no queued answer starts playing.
            if listen.hold_runs is not None:
                listen.hold_runs(held)
            if tts is not None:
                tts.hold_output(held=held)
            if held:
                # His words will end in a request; have its connection open by then.
                if listen.warm is not None:
                    listen.warm()
                pipeline.warm_input_model()

        def _dump_echo_history(canceller: voice_aec.EchoCanceller) -> None:
            path = canceller.dump(runtime.runtime_paths.root / "aec-diagnostics")
            if path is not None:
                LOGGER.info("echo diagnostics written: %s (%s)", path, canceller.summary())

        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=vad,
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=(tts.is_output_active if tts is not None else None),
            wake_threshold=knobs.wake_threshold,
            config=session_config,
            mic_muted=mic_muted,
            conversation=conversation,
            set_conversation=set_conversation,
            set_quiet=set_quiet,
            answer_words=listen.answer_words,
            turn_working=listen.turn_working,
            recent_speech=listen.recent_speech,
            ask_words=listen.ask_words,
            note_words=listen.note_words,
            cues=listen.cues,
            begin_line=listen.begin_line,
            stop_speaking=_stop_speaking,
            hold_output=_hold_output,
            supersede_unspoken=listen.supersede_unspoken,
            cancel_voice_runs=listen.cancel_voice_runs,
            heard_elsewhere=listen.heard_elsewhere,
            voiceprints=listen.voiceprints,
            judged_on_send=listen.judged_on_send,
            yield_speaking=tts.set_yield_gain if tts is not None else None,
            pause_speaking=tts.pause_speaking if tts is not None else None,
            ambient=listen.ambient,
        )
    except Exception as exc:
        LOGGER.exception(
            "realtime.single_audio_ingress construction failed before device open; "
            "text and PTT upload only.",
        )
        if ingress is not None:
            with contextlib.suppress(Exception):
                ingress.close()
        with contextlib.suppress(Exception):
            engine.close()
        return SessionAttempt(None, why=f"the capture session was not built: {exc!r}")
    try:
        start_result = session.start()
    except voice_session.VoiceSessionPreDeviceError as exc:
        LOGGER.exception(
            "realtime.single_audio_ingress Silero prepare failed before device "
            "open; text and PTT upload only.",
        )
        with contextlib.suppress(Exception):
            session.close()
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason="session_prepare_failed_before_device",
            fallback="text_ptt_upload",
            input_owner_attempted=False,
        )
        return SessionAttempt(None, why=f"the session was not prepared: {exc!r}")
    except Exception as exc:
        LOGGER.exception(
            "realtime.single_audio_ingress failed after device ownership attempt; preserving "
            "text/PTT-upload only and refusing a second input owner.",
        )
        return SessionAttempt(
            None, attempted=True, definitively_closed=_closed_for_good(session),
            why=f"the session's start raised: {exc!r}",
        )
    capability = start_result.ingress.capability
    if not start_result.started:
        LOGGER.warning(
            "realtime.single_audio_ingress start failed state=%s reason=%s; "
            "preserving text/PTT-upload only and refusing a second input owner.",
            capability.state.value,
            capability.reason,
        )
        close_result = session.close()
        record_realtime_trace(
            "audio_input_activation_downgraded",
            reason=capability.reason,
            fallback="text_ptt_upload",
            input_owner_attempted=True,
            definitively_closed=close_result.definitively_closed,
        )
        return SessionAttempt(
            None, attempted=True, definitively_closed=close_result.definitively_closed,
            why=f"state={capability.state.value} reason={capability.reason}",
        )
    return SessionAttempt(session, attempted=True)


def _closed_for_good(session: voice_session.DuplexVoiceSession) -> bool:
    """Close a session whose start failed; whether that confirmed the microphone free."""
    try:
        return session.close().definitively_closed
    except Exception:
        LOGGER.exception("realtime.single_audio_ingress close after a failed start also failed")
        return False


def _input_owner_reason(
    runtime: JarvisRuntime,
    *,
    tts: StreamingTTSPipeline | None,
    duplex_session: voice_session.DuplexVoiceSession | None,
    attempted: bool,
) -> str:
    """Name why this boot ended up with the input owner it has.

    Re-runs :func:`single_ingress_activation`, which is a pure read of
    ``runtime.config.raw`` and ``tts``: cheaper once per boot than threading a
    third return value through a helper five tests construct directly.
    """
    reason = single_ingress_activation(runtime, tts=tts).reason
    if duplex_session is not None:
        return reason
    if attempted:
        # A device open was tried and did not survive; the specific failure is
        # already an ``audio_input_activation_downgraded`` trace.
        return "single_ingress_start_failed"
    # Single ingress declined before it touched a device, for `reason`.
    return reason


def spawn_voice_input_owners(  # noqa: PLR0913 - composition boundary dependencies
    *,
    runtime: JarvisRuntime,
    pipeline: voice_pipeline.VoicePipeline,
    broadcaster: InherentBroadcaster,
    silero_path: Path,
    tts: StreamingTTSPipeline | None,
    voice: VoiceKnobs | None = None,
    mic_muted: Callable[[], bool] | None = None,
    conversation: Callable[[], bool] | None = None,
    set_conversation: Callable[[bool, str], None] | None = None,
    set_quiet: Callable[[str], None] | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
) -> VoiceInputOwners:
    """Start the single audio ingress, or leave the boot with text and PTT upload only."""
    knobs = VoiceKnobs() if voice is None else voice
    attempt = spawn_single_ingress_session(
        runtime=runtime,
        pipeline=pipeline,
        broadcaster=broadcaster,
        silero_path=silero_path,
        tts=tts,
        voice=knobs,
        mic_muted=mic_muted,
        conversation=conversation,
        set_conversation=set_conversation,
        set_quiet=set_quiet,
        echo_canceller=echo_canceller,
    )
    return VoiceInputOwners(
        duplex_session=attempt.session,
        single_ingress_attempted=attempt.attempted,
        reason=_input_owner_reason(
            runtime,
            tts=tts,
            duplex_session=attempt.session,
            attempted=attempt.attempted,
        ),
    )
