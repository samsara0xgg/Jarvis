"""The brain's half of a voice terminal or phone: its voice, its speech and its barge-in."""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from jarvis.runtime import JarvisRuntime, device_voice
from jarvis.runtime.daemon.output_watchers import PhoneRows, SpokenRows
from jarvis.runtime.daemon.voice_wiring import VoiceKnobs, minimax_client, tts_volume_kwargs
from jarvis.runtime.phone_voice import PhoneSpeech
from jarvis.runtime.turn.cancel import (
    make_barge_in_interrupt_callable,
    make_foreground_decision_callable,
)
from jarvis.state.event_log import open_runtime_event_log
from jarvis.surface import terminal_voice, voice_artifact_store, voice_media
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneAudioStreamPlayer

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

LOGGER = logging.getLogger(__name__)


def build_brain_voice(
    runtime: JarvisRuntime, knobs: VoiceKnobs,
) -> terminal_voice.BrainVoice | None:
    """What lets a brain speak to a voice terminal: the key, and the answer's rows (ADR 0172).

    ``None`` without ``MINIMAX_API_KEY``: as on one machine, no key is no speech, and the
    terminal is told so when it connects.
    """
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning("MINIMAX_API_KEY unset; a voice terminal will not be spoken to.")
        return None
    realtime_raw = runtime.config.raw.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    recorder = (
        voice_artifact_store.TtsRecorder(runtime.memory.audio_dir)
        if runtime.config.diagnostics.record_tts_audio and runtime.memory is not None
        else None
    )
    client = minimax_client(
        knobs, api_key, tts_volume_kwargs(realtime),
        recorder=recorder,
        voice_settings=runtime.voice_settings,
    )
    return terminal_voice.BrainVoice(
        client,
        SpokenRows(
            runtime.conn,
            None if runtime.response_runs is None else runtime.response_runs.turn_text_only,
        ),
    )


def phone_barge_in(runtime: JarvisRuntime, device: str) -> str:
    """The local barge-in's generation interrupt, aimed at ``device``'s open answer alone."""
    only_turns = device_voice.device_turns(runtime.runtime_paths.event_log, device)
    return make_barge_in_interrupt_callable(runtime, only_turns=only_turns)("phone_barge")


def build_phone_speech(
    runtime: JarvisRuntime, knobs: VoiceKnobs, rows: PhoneRows,
) -> PhoneSpeech | None:
    """What gives a voice phone its own media actor: the key and the streaming speech config.

    ``None`` without ``MINIMAX_API_KEY`` or without the streaming pipeline's own preconditions
    (``_build_tts_pipeline``'s): the phone is then told ``voice: false`` and gets rows only.
    The actor runs at the phone's 32 kHz so nothing is resampled, and without the Mac's
    fallbacks: ``say`` would speak on this machine's speaker, and a cut-off line is the
    Mac's to prefetch.
    """
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        LOGGER.warning("MINIMAX_API_KEY unset; a phone will not be spoken to.")
        return None
    realtime_raw = runtime.config.raw.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    try:
        base = voice_media.streaming_media_config_from_mapping(streaming)
    except ValueError as exc:
        LOGGER.warning("realtime.streaming_output config invalid (%s); a phone gets no voice.", exc)
        return None
    if not runtime.config.realtime.enabled:
        LOGGER.warning("streaming speech is off; a phone gets no voice.")
        return None
    config = dataclasses.replace(
        base,
        canonical_sample_rate_hz=PHONE_SAMPLE_RATE_HZ,
        enable_macos_say_fallback=False,
        prefetch_network_lost_line=False,
    )
    volume_kwargs = tts_volume_kwargs(realtime)
    event_log_path = runtime.runtime_paths.event_log

    def build(
        player: PhoneAudioStreamPlayer,
        speaks_turn: Callable[[sqlite3.Connection, str], bool],
        boot_high_water_id: int,
    ) -> voice_media.StreamingTTSPipeline | None:
        provider = minimax_client(
            knobs, api_key, volume_kwargs,
            recorder=None, voice_settings=runtime.voice_settings,
        )
        try:
            return voice_media.StreamingTTSPipeline(
                provider=provider,
                player=player,
                conn_factory=lambda: open_runtime_event_log(event_log_path),
                boot_high_water_id=boot_high_water_id,
                config=config,
                foreground_decision_callable=make_foreground_decision_callable(
                    wait_for_lane=runtime.response_flags.slow_results,
                ),
                start_player=False,
                speaks_turn=speaks_turn,
            )
        except Exception:
            LOGGER.exception("a phone's media actor could not start")
            return None

    return PhoneSpeech(
        build=build, rows=rows, ring_seconds=config.ring_seconds, event_log=event_log_path,
    )


def per_device[T](make: Callable[[str], T]) -> Callable[[str], T]:
    """``make`` for a device, built on first use and kept: a terminal's hooks are asked often."""
    made: dict[str, T] = {}

    def get(device: str) -> T:
        if device not in made:
            made[device] = make(device)
        return made[device]

    return get
