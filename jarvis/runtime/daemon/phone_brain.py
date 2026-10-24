"""The brain's half of a voice terminal or phone: its voice, its speech and its barge-in."""

from __future__ import annotations

import dataclasses
import functools
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from jarvis.runtime import JarvisRuntime, device_voice
from jarvis.runtime.daemon.listen_ports import local_listen_ports, make_cancel_voice_runs
from jarvis.runtime.daemon.output_watchers import PhoneRows, SpokenRows
from jarvis.runtime.daemon.submissions import resolve_topic
from jarvis.runtime.daemon.voice_wiring import VoiceKnobs, minimax_client, tts_volume_kwargs
from jarvis.runtime.dictation import local_polisher
from jarvis.runtime.phone_voice import PhoneSpeech
from jarvis.runtime.turn.cancel import (
    make_barge_in_interrupt_callable,
    make_compose_callable,
    make_continuation_callable,
    make_foreground_decision_callable,
    make_relation_supersede_callable,
    make_supersede_unspoken_callable,
    make_turn_cancel_callable,
)
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.event_log import open_runtime_event_log
from jarvis.surface import terminal_listen, terminal_voice, voice_artifact_store
from jarvis.surface.phone_link import PhoneHub
from jarvis.surface.phone_player import PHONE_SAMPLE_RATE_HZ, PhoneAudioStreamPlayer
from jarvis.surface.voice_media.config import streaming_media_config_from_mapping
from jarvis.surface.voice_media.pipeline import StreamingTTSPipeline

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.surface import voice_controls
    from jarvis.surface.terminal_link import TerminalHub

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
        base = streaming_media_config_from_mapping(streaming)
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
    ) -> StreamingTTSPipeline | None:
        provider = minimax_client(
            knobs, api_key, volume_kwargs,
            recorder=None, voice_settings=runtime.voice_settings,
        )
        try:
            return StreamingTTSPipeline(
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


def build_phone_hub(
    runtime: JarvisRuntime, knobs: VoiceKnobs, *, set_quiet: Callable[[str], None] | None,
) -> tuple[PhoneHub, Callable[[str], int | None]] | None:
    """The phone's conversation socket (ADR 0209), on every host that listens.

    ``None`` where the host has nowhere to write a phone's words. A voice phone gets its own
    media actor when the host can speak; barge-in and cancel reach only that device's turns.
    Words it hears over her are judged with the Mac's own hooks (ADR 0216,
    :func:`local_listen_ports`) and ``set_quiet``, the daemon's, which the caller passes in.
    The second value says which log row a device's actor was built at (``None``: it has none),
    for :func:`phone_ends_watcher`. The hub is also what the locator asks for a fix over a
    device's socket and what the whereabouts reads to know a phone is open.
    """
    events = runtime.phone_events
    if events is None:
        return None
    rows = PhoneRows(runtime.conn)
    speech = build_phone_speech(runtime, knobs, rows)
    registry = runtime.response_runs
    mine = local_listen_ports(runtime, None)
    hub = PhoneHub(
        events=events,
        rows=rows,
        open_voice=None if speech is None else speech.open,
        barge_in=None if registry is None else functools.partial(phone_barge_in, runtime),
        cancel_turn=None if registry is None else make_turn_cancel_callable(runtime),
        supersede_turn=None if registry is None else make_continuation_callable(runtime),
        pick_earlier=None if registry is None else make_compose_callable(runtime),
        ask_words=mine.ask_words,
        note_words=mine.note_words,
        begin_line=mine.begin_line,
        recent_speech=mine.recent_speech,
        set_quiet=set_quiet,
        heard_elsewhere=lambda device, _turn_id, text: device_voice.heard_elsewhere(
            runtime.runtime_paths.event_log, device, text,
        ),
        voiceprints=runtime.voiceprints,
        check_attachments=(
            None if runtime.attachments is None else runtime.attachments.check_for_turn
        ),
        resolve_topic=(
            None if runtime.memory is None
            else functools.partial(resolve_topic, runtime.memory.db_path)
        ),
    )
    if runtime.locator is not None:
        # ADR 0281: a phone with a live conversation socket is asked for a fix over it.
        runtime.locator.ask_socket = hub.ask_location
    if runtime.whereabouts is not None:
        # ADR 0217: a device with its conversation socket open is a phone, and that is said.
        runtime.whereabouts.phone_open = hub.connected
    return hub, (lambda _device: None) if speech is None else speech.owner_boot


def build_brain_listening(
    runtime: JarvisRuntime,
    hub: TerminalHub,
    *,
    controls: voice_controls.VoiceControls,
    set_conversation: Callable[[bool, str], None],
    set_quiet: Callable[[str], None],
) -> terminal_listen.BrainListening | None:
    """What lets a brain take a voice terminal's listening: its words, its holds, its turns.

    The brain's half of each capture-session callback is the one a daemon runs for its own
    session (:func:`local_listen_ports`); only supersede and relate differ, since the audio
    they drop or stop is on the terminal that heard the line (ADR 0172). The voice-driven
    cancels (interrupt, supersede, cancel_runs) and ``heard_elsewhere`` take the terminal's
    name and act on the turns it opened alone (ADR 0219); ``hold_runs`` stays global.
    ``None`` without the brain's log, where an utterance has nowhere to go.
    """
    if hub.events is None:
        return None
    listening = terminal_listen.BrainListening(hub, hub.events)
    mine = local_listen_ports(runtime, None)
    registry = runtime.response_runs
    if runtime.oneshot is not None and registry is not None:
        # ADR 0139: a correction stops the answer that is audible, on the terminal that heard it.
        runtime.oneshot.relate = make_relation_supersede_callable(
            runtime, listening.drop_unspoken, lambda: listening.stop_output("correction"),
        )

    def _hold_runs(held: bool) -> None:  # noqa: FBT001 - the capture side's one bit
        if mine.hold_runs is not None:
            mine.hold_runs(held)
        if held and mine.warm is not None:
            mine.warm()  # his words will end in a request; have its connection open by then

    # ADR 0219: a terminal's voice acts on the turns that terminal opened, nothing else's.
    event_log_path = runtime.runtime_paths.event_log
    barge_in = per_device(
        lambda device: make_barge_in_interrupt_callable(
            runtime, only_turns=device_voice.device_turns(event_log_path, device),
        ),
    )
    compose = make_compose_callable(runtime)
    supersede = per_device(
        lambda device: make_supersede_unspoken_callable(
            runtime, listening.drop_unspoken,
            only_turns=device_voice.device_turns(event_log_path, device),
            # ADR 0278: the device's pending sentence is composed into the next one at its
            # utterance, so the sweep leaves it to that.
            leave=lambda turn_id: compose(device, turn_id, None),
        ),
    )
    cancel_runs = per_device(
        lambda device: make_cancel_voice_runs(
            runtime, only_turns=device_voice.device_turns(event_log_path, device),
        ),
    )

    listening.hooks = terminal_listen.ListenHooks(
        ask_words=mine.ask_words,
        note_words=mine.note_words,
        begin_line=mine.begin_line,
        answer_words=mine.answer_words,
        turn_working=mine.turn_working,
        recent_speech=mine.recent_speech,
        hold_runs=_hold_runs,
        interrupt=lambda device, source: barge_in(device)(source),
        supersede=(
            (lambda device, turn_id: supersede(device)(turn_id))
            if registry is not None
            else None
        ),
        cancel_runs=(
            (lambda device: cancel_runs(device)()) if registry is not None else None
        ),
        composes_into=compose if registry is not None else None,
        take_over=make_continuation_callable(runtime) if registry is not None else None,
        heard_elsewhere=lambda device, _turn_id, text: device_voice.heard_elsewhere(
            event_log_path, device, text,
        ),
        voiceprints=runtime.voiceprints,
        set_conversation=set_conversation,
        set_quiet=set_quiet,
        controls=controls.update,
        # ADR 0183: a terminal's dictation is polished here, with the key only the brain has.
        polish=local_polisher(
            runtime.config.raw, runtime.runtime_paths.event_log,
            load_pricing_table(runtime.config.pricing_path),
        ),
    )
    return listening
