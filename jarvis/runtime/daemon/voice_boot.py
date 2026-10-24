"""The voice subsystem at boot: what is built, the input owner, GPT-Live (ADR 0006, 0016)."""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jarvis.runtime.daemon.controls import set_conversation, set_quiet
from jarvis.runtime.daemon.devices import output_or_default
from jarvis.runtime.daemon.input_session import spawn_voice_input_owners
from jarvis.runtime.daemon.live_backend import LiveBackend, live_wake_subscriber
from jarvis.runtime.daemon.phone_brain import (
    build_brain_hearing,
    build_brain_listening,
    build_brain_voice,
)
from jarvis.runtime.daemon.restart import fetch_models_then_restart
from jarvis.runtime.daemon.voice_power import VoiceInputOwners
from jarvis.runtime.daemon.voice_wiring import (
    build_echo_canceller,
    build_tts_pipeline,
    build_voice_pipeline,
    build_voice_pipeline_callable,
    voice_models_preflight,
)
from jarvis.runtime.dictation import build_dictation, local_polisher
from jarvis.shared.pricing import load_pricing_table
from jarvis.surface import voice_ducking, voice_live

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.deployment import models
    from jarvis.runtime import JarvisRuntime
    from jarvis.runtime.daemon.voice_wiring import VoiceKnobs
    from jarvis.runtime.dictation import Dictation
    from jarvis.shared import Event
    from jarvis.surface import voice_audio, voice_controls, voice_pipeline
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.voice_media.pipeline import StreamingTTSPipeline

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class Voice:
    """What the voice subsystem is, as far as boot got: ``None`` is what was not built.

    Filled by :func:`boot_voice` and :func:`boot_dictation`; what the mute switches, the HTTP
    routes, the watchers and the teardown read late, so each reads it from here and sees what
    exists at the moment it asks.
    """

    controls: voice_controls.VoiceControls
    # ADR-0005 §5.1 / §5.3: ONE shared SystemAudioDucker counts the TTS
    # provider/playback output leases the night run waits on.
    ducker: voice_ducking.SystemAudioDucker = dataclasses.field(
        default_factory=voice_ducking.SystemAudioDucker,
    )
    pipe: voice_pipeline.VoicePipeline | None = None
    pipeline_callable: Callable[[bytes, str, str, str], Event] | None = None
    tts: StreamingTTSPipeline | None = None
    owners: VoiceInputOwners = dataclasses.field(
        default_factory=lambda: VoiceInputOwners(
            duplex_session=None,
            single_ingress_attempted=False,
        ),
    )
    live: voice_live.LiveVoice | None = None
    dictation: Dictation | None = None

    def input_blocked(self) -> bool:
        """Whether the local chain arms no new wake right now."""
        # ADR-0015 mic mute, plus: while GPT-Live owns speech the local chain
        # arms no new wake, so one utterance cannot be answered twice; and
        # while Allen dictates (ADR 0058) his words are text, not a turn.
        return (
            self.controls.mic_is_muted()
            or (self.live is not None and self.live.owns_speech)
            or (self.dictation is not None and self.dictation.active)
        )

    def apply_speech_mute(self, muted: bool) -> None:  # noqa: FBT001 - Callable[[bool], None] shape
        """Set the player's gain and tell GPT-Live (``controls.on_speech_muted``)."""
        # ADR-0015 D2: speech mute is the player's output gain, 0.0 muted and
        # 1.0 restored; synthesis, timing, phases and events run as unmuted.
        # The local player is also silent while GPT-Live owns speech.
        if self.tts is not None:
            local_silent = muted or (self.live is not None and self.live.owns_speech)
            self.tts.set_output_gain(0.0 if local_silent else 1.0)
        if self.live is not None:
            self.live.set_speech_muted(muted)


def boot_voice(
    runtime: JarvisRuntime,
    voice: Voice,
    *,
    broadcaster: InherentBroadcaster,
    knobs: VoiceKnobs,
    model_fetch: models.Progress,
) -> None:
    """Wire the voice terminal of a brain, build the local voice, GPT-Live, and log what resolved.

    ADR-0005 §12 pre-flight + voice subsystem wiring. Any failure downgrades the daemon to
    text-only: the text path must stay healthy when models / SDKs / mics are missing (CI default).
    """
    if runtime.terminal_hub is not None:
        runtime.terminal_hub.voice = build_brain_voice(runtime, knobs)
        runtime.terminal_hub.hearing = build_brain_hearing()
        runtime.terminal_hub.listening = build_brain_listening(
            runtime, runtime.terminal_hub,
            controls=voice.controls,
            set_conversation=functools.partial(set_conversation, voice.controls, broadcaster),
            set_quiet=functools.partial(set_quiet, voice.controls),
        )
    sensevoice_dir = runtime.config.models.sensevoice_dir
    silero_path = runtime.config.models.silero_vad_path
    brain = runtime.config.role == "brain"
    models_ok, missing = (
        (False, [])
        if brain
        else voice_models_preflight(sensevoice_dir=sensevoice_dir, silero_path=silero_path)
    )
    reason = "models_missing"
    if brain:
        # ADR 0170: the microphone, wake, ASR and playback belong to a terminal.
        reason = "role_brain"
    elif not models_ok:
        LOGGER.error(
            "voice models missing; running text-only. Missing: %s",
            "; ".join(missing),
        )
        fetch_models_then_restart(
            sensevoice_dir, silero_path, asyncio.get_running_loop(), model_fetch,
        )
    else:
        try:
            reason = _build_local_voice(runtime, voice, broadcaster, knobs, silero_path)
        except Exception:
            reason = "voice_construction_failed"
            LOGGER.exception(
                "voice subsystem construction failed; running text-only.",
            )
            voice.pipe = None
            voice.pipeline_callable = None
            voice.tts = None
            voice.owners = VoiceInputOwners(
                duplex_session=None,
                single_ingress_attempted=False,
            )

    _boot_live(runtime, voice, broadcaster)

    # One record per boot on EVERY path above — models present, models
    # missing, construction failed — so "what did this daemon resolve, and
    # which input owner does it have" never needs a grep again.
    _log_voice_startup(
        knobs=knobs,
        sensevoice_dir=sensevoice_dir,
        silero_vad_path=silero_path,
        models_ok=models_ok,
        owners=voice.owners,
        reason=reason,
    )


def _build_local_voice(
    runtime: JarvisRuntime,
    voice: Voice,
    broadcaster: InherentBroadcaster,
    knobs: VoiceKnobs,
    silero_path: Path,
) -> str:
    """Build the pipeline, the speech and the input owner; return why the owner is what it is."""
    voice.pipe = build_voice_pipeline(
        runtime,
        broadcaster=broadcaster,
        sensevoice_dir=runtime.config.models.sensevoice_dir,
        voiceprint_model=runtime.config.models.voiceprint_model,
    )
    voice.pipeline_callable = build_voice_pipeline_callable(voice.pipe)
    echo_canceller = build_echo_canceller(runtime)
    voice.tts = build_tts_pipeline(
        runtime,
        broadcaster,
        ducker=voice.ducker,
        voice=knobs,
        echo_canceller=echo_canceller,
    )
    if os.environ.get("JARVIS_VOICE_DISABLE_WAKE") == "1":
        LOGGER.info(
            "JARVIS_VOICE_DISABLE_WAKE=1; skipping the microphone owner.",
        )
        return "wake_disabled_env"
    voice.owners = spawn_voice_input_owners(
        runtime=runtime,
        pipeline=voice.pipe,
        broadcaster=broadcaster,
        silero_path=silero_path,
        tts=voice.tts,
        voice=knobs,
        mic_muted=voice.input_blocked,
        conversation=voice.controls.conversation_is_on,
        set_conversation=functools.partial(set_conversation, voice.controls, broadcaster),
        set_quiet=functools.partial(set_quiet, voice.controls),
        echo_canceller=echo_canceller,
    )
    return voice.owners.reason


def _boot_live(runtime: JarvisRuntime, voice: Voice, broadcaster: InherentBroadcaster) -> None:
    """GPT-Live (ADR-0016). Building the controller opens nothing.

    A session starts only from ``POST /inherent/controls {"live": "start"}``, which re-checks
    the single audio ingress and the API key at that moment. Billed per second, so the shutdown
    hangs up before the microphone goes away.
    """
    controls = voice.controls
    realtime_raw = runtime.config.raw.get("realtime")
    realtime_map = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    gpt_live_raw = realtime_map.get("gpt_live")
    gpt_live_config: voice_live.GptLiveConfig | None = None
    if isinstance(gpt_live_raw, Mapping):
        try:
            gpt_live_config = voice_live.gpt_live_config_from_mapping(
                {"assistant": runtime.config.assistant_name, **gpt_live_raw},
            )
        except (TypeError, ValueError):
            LOGGER.exception("realtime.gpt_live is malformed; GPT-Live stays off this boot")
    if gpt_live_config is None or not gpt_live_config.enabled:
        return

    def live_ingress() -> voice_audio.AudioIngress | None:
        session = voice.owners.duplex_session
        return session.ingress if session is not None else None

    # ADR-0016 D9: the composition root is the only place that wires
    # the L5 session to L2 (inbox, memory.db, Event Log) and to the bus.
    live_backend = LiveBackend(
        event_log_path=runtime.runtime_paths.event_log,
        memory=runtime.memory,
        session=runtime.config.session,
    )
    voice.live = voice_live.LiveVoice(
        config=gpt_live_config,
        broadcaster=broadcaster,
        ingress=live_ingress,
        mic_muted=controls.mic_is_muted,
        speech_muted=lambda: controls.speech_muted,
        output_device=output_or_default(realtime_map.get("output_device")),
        volume=float(realtime_map.get("playback_volume") or 1.0),
        on_owns_speech=lambda _owns: voice.apply_speech_mute(controls.speech_muted),
        delegate=live_backend.delegate,
        record=live_backend.record,
        brief=live_backend.brief,
        lookup_result=live_backend.lookup_result,
        undelivered=live_backend.undelivered_results,
        mark_delivered=live_backend.mark_delivered,
        record_usage=live_backend.record_usage,
    )
    controls.on_mic_muted = voice.live.set_mic_muted
    if runtime.committed_event_bus is not None:
        runtime.committed_event_bus.subscribe(live_wake_subscriber(voice.live))
    else:
        LOGGER.info("gpt_live: no committed-event bus; delegations rely on the poll")
    if not os.environ.get(gpt_live_config.api_key_env):
        LOGGER.warning(
            "realtime.gpt_live.enabled but %s is unset; start will be refused",
            gpt_live_config.api_key_env,
        )


def boot_dictation(runtime: JarvisRuntime, voice: Voice) -> None:
    """ADR 0058: dictation hears through the live mic and the voice path's ears."""
    session = voice.owners.duplex_session
    ingress = session.ingress if session is not None else None
    if ingress is None or voice.pipe is None:
        return
    polisher = local_polisher(
        runtime.config.raw, runtime.runtime_paths.event_log,
        load_pricing_table(runtime.config.pricing_path),
    )
    if polisher is not None:
        voice.dictation = build_dictation(
            config=runtime.config.raw, ingress=ingress,
            silero_path=runtime.config.models.silero_vad_path,
            transcribe=voice.pipe.transcribe, polisher=polisher,
            vocab_path=polisher.vocab_path,
            recordings=(
                runtime.memory.audio_dir
                if runtime.memory is not None and runtime.memory.retain_audio
                else None
            ),
        )


def _log_voice_startup(  # noqa: PLR0913 - the record's fields ARE its contract
    *,
    knobs: VoiceKnobs,
    sensevoice_dir: Path,
    silero_vad_path: Path,
    models_ok: bool,
    owners: VoiceInputOwners,
    reason: str,
) -> None:
    """Emit the one per-boot record naming every resolved voice value.

    ADR-0006 §5 — the point of moving these values into config is that one file
    decides them, which is only checkable if the daemon says what it resolved.
    JSON rather than ``key=value`` because there are twenty-nine fields.

    Also the place startup says whether an input owner is running at all.
    """
    input_owner = "single_ingress" if owners.duplex_session is not None else "none"
    payload: dict[str, Any] = {
        "sensevoice_dir": str(sensevoice_dir),
        "silero_vad_path": str(silero_vad_path),
        "models_ok": models_ok,
        "input_owner": input_owner,
        "reason": reason,
    }
    payload.update(
        {
            field.name: getattr(knobs, field.name)
            for field in dataclasses.fields(knobs)
            if field.name != "vad_profiles"
        },
    )
    for mode, profile in sorted(knobs.vad_profiles.items()):
        payload.update(
            {
                f"vad_{mode}_{key}": value
                for key, value in dataclasses.asdict(profile).items()
            },
        )
    LOGGER.info("voice startup config: %s", json.dumps(payload, sort_keys=True))
