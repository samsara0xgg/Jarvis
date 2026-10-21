"""Voice wiring: the knobs and model preflight, the pipeline builders and the ingress decision."""

from __future__ import annotations

import dataclasses
import functools
import logging
import math
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.runtime.daemon.devices import output_or_default
from jarvis.runtime.daemon.output_watchers import TTS_SILENT_CHANNELS
from jarvis.runtime.daemon.voice_power import SingleIngressActivation
from jarvis.runtime.dictation import COMMAND_PROMPT, load_user_terms, whisper_ears
from jarvis.runtime.turn.cancel import make_foreground_decision_callable
from jarvis.state.event_log import latest_id, open_runtime_event_log
from jarvis.surface import (
    voice_aec,
    voice_artifact_store,
    voice_asr,
    voice_audio,
    voice_ducking,
    voice_media,
    voice_native_out,
    voice_pipeline,
    voice_session,
    voice_tts,
)
from jarvis.surface.voice_print import load_extractor

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from jarvis.decision.response_run import ResponseRunRegistry
    from jarvis.runtime.config import RuntimeConfig
    from jarvis.shared import Event
    from jarvis.shared.realtime import Wave4ResponseFlags
    from jarvis.state.memory_db import MemorySettings
    from jarvis.state.voice_settings import VoiceSettings
    from jarvis.state.voiceprint import VoiceprintBook
    from jarvis.surface.ambient_sounds import AmbientSounds
    from jarvis.surface.inherent_output import InherentBroadcaster
    from jarvis.surface.voice_cues import VoiceCues

LOGGER = logging.getLogger(__name__)


# The voice ASR / VAD artifact locations come from ``config.models`` (ADR-0006 §5),
# resolved once at load against the config file's own directory. ADR-0005 §12 (pre-flight) —
# ``serve_inherent`` checks them BEFORE starting the microphone owner so a missing
# wheel surfaces as a single log line instead of a crashed daemon thread on
# first wake. Tests pin their own paths via :func:`voice_models_preflight`
# kwargs.

# The two ``SileroVad`` profiles the daemon can bind (``voice_audio``'s
# ``_MODE_THRESHOLDS`` keys).  A closed set: ADR-0006 D8 names both and
# ``realtime.single_audio_ingress.output_active_vad_mode`` selects between them.
_VAD_MODES: Final[tuple[str, ...]] = ("record", "tts")


def _default_vad_profiles() -> dict[str, voice_audio.VadThresholds]:
    """Return the shipped per-mode thresholds — L5 stays their single source."""
    return {mode: voice_audio.SileroVad.thresholds(mode) for mode in _VAD_MODES}


@dataclasses.dataclass(frozen=True)
class VoiceKnobs:
    """Every flat ``realtime:`` voice value, resolved once (ADR-0006 §5, :716).

    The field defaults ARE the shipped constants, so a config that sets none of
    these produces an instance indistinguishable from the hard-coded daemon —
    the property the whole change is required to preserve.  Parsed once in
    :func:`serve_inherent` and threaded from there; nothing below the
    composition root reads ``realtime:`` for these values.
    """

    # The engine's detection probability gate.
    wake_threshold: float = 0.5
    tts_voice: str = voice_tts.DEFAULT_TTS_VOICE
    tts_model: str = voice_tts.DEFAULT_TTS_MODEL
    tts_primary_endpoint: str = voice_tts.DEFAULT_TTS_PRIMARY_ENDPOINT
    tts_fallback_endpoint: str = voice_tts.DEFAULT_TTS_FALLBACK_ENDPOINT
    # MiniMax is asked for the highest rate it natively produces in our config
    # and resampled to the device rate via soxr on the way out.
    tts_sample_rate_in_hz: int = 32000
    tts_connect_timeout_s: float = voice_tts.DEFAULT_TTS_CONNECT_TIMEOUT_S
    tts_first_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_FIRST_CHUNK_TIMEOUT_S
    tts_between_chunk_timeout_s: float = voice_tts.DEFAULT_TTS_BETWEEN_CHUNK_TIMEOUT_S
    vad_profiles: Mapping[str, voice_audio.VadThresholds] = dataclasses.field(
        default_factory=_default_vad_profiles,
    )


def _knob_number(values: Mapping[str, Any], key: str, fallback: float) -> float:
    """Return a positive numeric ``realtime.<key>``; ``fallback`` with a warning.

    Degrade rather than raise: these are read at the composition root, outside
    every downgrade boundary the daemon has, so a typo must not cost the boot.
    ``bool`` is excluded explicitly because it is an ``int`` subclass.
    """
    raw = values.get(key)
    if raw is None:
        return fallback
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int | float)
        or not math.isfinite(raw)
        or raw <= 0
    ):
        LOGGER.warning(
            "realtime.%s must be a positive finite number; using %s.", key, fallback,
        )
        return fallback
    return float(raw)


def _knob_text(values: Mapping[str, Any], key: str, fallback: str) -> str:
    """Return a non-empty string ``realtime.<key>``; ``fallback`` with a warning."""
    raw = values.get(key)
    if raw is None:
        return fallback
    if not isinstance(raw, str) or not raw.strip():
        LOGGER.warning(
            "realtime.%s must be a non-empty string; using %r.", key, fallback,
        )
        return fallback
    return raw.strip()


def _knob_count(
    values: Mapping[str, Any],
    key: str,
    fallback: int,
    *,
    label: str | None = None,
) -> int:
    """Return a positive integer ``realtime.<key>``; ``fallback`` with a warning.

    ``label`` names the key in the warning when it differs from the lookup —
    a nested profile field is looked up as ``required_hits`` but must be
    reported as ``realtime.vad.record.required_hits`` to be actionable.
    """
    raw = values.get(key)
    if raw is None:
        return fallback
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        LOGGER.warning(
            "realtime.%s must be a positive integer; using %d.",
            key if label is None else label,
            fallback,
        )
        return fallback
    return raw


def _vad_profile(raw: object, *, mode: str, fallback: voice_audio.VadThresholds,
                 ) -> voice_audio.VadThresholds:
    """Parse one ``realtime.vad.<mode>`` profile; each field degrades alone."""
    if raw is None:
        return fallback
    if not isinstance(raw, Mapping):
        LOGGER.warning("realtime.vad.%s must be a mapping; using defaults.", mode)
        return fallback
    values: Mapping[str, Any] = raw
    prob = values.get("prob_threshold")
    db = values.get("db_threshold")
    if prob is not None and (
        isinstance(prob, bool) or not isinstance(prob, int | float) or not 0.0 < prob <= 1.0
    ):
        LOGGER.warning(
            "realtime.vad.%s.prob_threshold must be in (0, 1]; using %s.",
            mode,
            fallback.prob_threshold,
        )
        prob = None
    if db is not None and (isinstance(db, bool) or not isinstance(db, int | float)):
        LOGGER.warning(
            "realtime.vad.%s.db_threshold must be a number; using %s.",
            mode,
            fallback.db_threshold,
        )
        db = None
    return voice_audio.VadThresholds(
        prob_threshold=fallback.prob_threshold if prob is None else float(prob),
        db_threshold=fallback.db_threshold if db is None else float(db),
        smoothing_window=_knob_count(
            values,
            "smoothing_window",
            fallback.smoothing_window,
            label=f"vad.{mode}.smoothing_window",
        ),
        required_hits=_knob_count(
            values,
            "required_hits",
            fallback.required_hits,
            label=f"vad.{mode}.required_hits",
        ),
        required_misses=_knob_count(
            values,
            "required_misses",
            fallback.required_misses,
            label=f"vad.{mode}.required_misses",
        ),
    )


def _vad_profiles(raw: object) -> dict[str, voice_audio.VadThresholds]:
    """Parse ``realtime.vad``; both profiles must share their debounce triple.

    ``DuplexVoiceSession`` calls :meth:`SileroVad.set_mode` on every
    output-active transition, i.e. mid-utterance.  That is only safe because
    ``reset`` sizes the smoothing deques from ``smoothing_window`` and the
    assembler snapshots ``required_misses`` once, so two profiles that disagree
    on either would desynchronise both.  A config that breaks the invariant is
    refused whole rather than half-applied.
    """
    defaults = _default_vad_profiles()
    if raw is None:
        return defaults
    if not isinstance(raw, Mapping):
        LOGGER.warning("realtime.vad must be a mapping; using defaults.")
        return defaults
    parsed = {
        mode: _vad_profile(raw.get(mode), mode=mode, fallback=defaults[mode])
        for mode in _VAD_MODES
    }
    debounce = {
        (p.smoothing_window, p.required_hits, p.required_misses) for p in parsed.values()
    }
    if len(debounce) > 1:
        LOGGER.warning(
            "realtime.vad profiles disagree on smoothing_window/required_hits/"
            "required_misses; the session switches profiles mid-utterance and "
            "cannot resize its debounce state. Using defaults for both.",
        )
        return defaults
    return parsed


def voice_knobs(config: Mapping[str, Any]) -> VoiceKnobs:
    """Resolve every flat ``realtime:`` voice value once (ADR-0006 §5)."""
    block = config.get("realtime")
    values: Mapping[str, Any] = block if isinstance(block, Mapping) else {}
    d = VoiceKnobs()
    return VoiceKnobs(
        wake_threshold=_knob_number(values, "wake_threshold", d.wake_threshold),
        tts_voice=_knob_text(values, "tts_voice", d.tts_voice),
        tts_model=_knob_text(values, "tts_model", d.tts_model),
        tts_primary_endpoint=_knob_text(
            values, "tts_primary_endpoint", d.tts_primary_endpoint,
        ),
        tts_fallback_endpoint=_knob_text(
            values, "tts_fallback_endpoint", d.tts_fallback_endpoint,
        ),
        tts_sample_rate_in_hz=_knob_count(
            values, "tts_sample_rate_in_hz", d.tts_sample_rate_in_hz,
        ),
        tts_connect_timeout_s=_knob_number(
            values, "tts_connect_timeout_s", d.tts_connect_timeout_s,
        ),
        tts_first_chunk_timeout_s=_knob_number(
            values, "tts_first_chunk_timeout_s", d.tts_first_chunk_timeout_s,
        ),
        tts_between_chunk_timeout_s=_knob_number(
            values, "tts_between_chunk_timeout_s", d.tts_between_chunk_timeout_s,
        ),
        vad_profiles=_vad_profiles(values.get("vad")),
    )


def voice_models_preflight(
    *,
    sensevoice_dir: Path,
    silero_path: Path,
) -> tuple[bool, list[str]]:
    """Verify on-disk model artifacts before starting the microphone owner.

    ADR-0005 §12 — pre-flight semantic: do NOT attempt to construct a
    SenseVoice recognizer or Silero VAD when the underlying ``.onnx``
    files are not present. Returning ``(False, [...])`` lets
    :func:`serve_inherent` log one ERROR and keep the text path
    running; the alternative (constructing the recognizer eagerly)
    would let the input thread crash on its first inference frame and
    pollute the daemon log with an unhelpful traceback.

    Returns ``(all_ok, missing_paths_list)``.
    """
    missing: list[str] = []
    sv_model = sensevoice_dir / "model.int8.onnx"
    sv_tokens = sensevoice_dir / "tokens.txt"
    if not sv_model.exists():
        missing.append(f"sensevoice-small-int8 model.int8.onnx (expected at {sv_model})")
    if not sv_tokens.exists():
        missing.append(f"sensevoice-small-int8 tokens.txt (expected at {sv_tokens})")
    if not silero_path.exists():
        missing.append(f"silero_vad.onnx (expected at {silero_path})")
    return (not missing, missing)


def final_recognizer(
    runtime: _ConfigHost, sensevoice: voice_asr.SenseVoiceRecognizer,
) -> voice_asr.AsrRecognizer:
    """``realtime.final_asr``: SenseVoice, Whisper as 言文 hears (proposal), or both (ADR 0132)."""
    realtime = runtime.config.raw.get("realtime")
    choice = realtime.get("final_asr") if isinstance(realtime, Mapping) else None
    if choice == "hybrid":
        # A loop is heard as nothing, so SenseVoice's words stand: a hotter retry took 4.6 s on
        # his laughter and still gave 886 characters of "beginning," (2026-10-03 15:56).
        # ADR 0150: the Chinese and English passes hear a short word list.
        listed = realtime.get("final_asr_terms") if isinstance(realtime, Mapping) else None
        terms = [str(t) for t in listed if str(t).strip()] if isinstance(listed, list) else []
        words = (lambda: terms) if terms else None
        whisper_zh = whisper_ears(language="zh", terms=words, retry_loops=False)
        whisper_en = whisper_ears(language="en", terms=words, retry_loops=False)
        if whisper_zh is None or whisper_en is None:
            LOGGER.warning("realtime.final_asr: hybrid needs mlx-whisper; hearing with SenseVoice")
            return sensevoice
        LOGGER.info("voice turns of 1 s or more hear with Whisper; SenseVoice keeps the rest")
        return voice_asr.HybridFinalRecognizer(
            sensevoice=sensevoice,
            whisper_zh=whisper_zh,
            whisper_en=whisper_en,
            whisper_command=whisper_ears(language="zh", prompt=COMMAND_PROMPT),
        )
    if choice != "whisper":
        return sensevoice
    dictation_config = runtime.config.raw.get("dictation") or {}
    vocab_path = Path(str(dictation_config.get("vocab_path", "")))
    whisper = whisper_ears(terms=functools.partial(load_user_terms, vocab_path))
    if whisper is None:
        LOGGER.warning("realtime.final_asr: whisper needs mlx-whisper; hearing with SenseVoice")
        return sensevoice
    LOGGER.info("voice turns hear with Whisper; SenseVoice keeps the partials")
    return voice_asr.WhisperFinalRecognizer(whisper=whisper, partials=sensevoice)


class _ConfigHost(Protocol):
    """A host of which the voice builders read only the loaded config."""

    @property
    def config(self) -> RuntimeConfig: ...


class _PipelineHost(_ConfigHost, Protocol):
    """What the voice pipeline's builder reads from its host: a runtime's, or a terminal's."""

    @property
    def runtime_paths(self) -> _EventLogPath: ...

    @property
    def memory(self) -> MemorySettings | None: ...

    @property
    def voice_cues(self) -> VoiceCues | None: ...


def build_voice_pipeline(
    runtime: _PipelineHost,
    *,
    broadcaster: InherentBroadcaster,
    sensevoice_dir: Path,
    emit: Callable[[Mapping[str, object], Mapping[str, str]], Event] | None = None,
    voiceprint_model: Path | None = None,
) -> voice_pipeline.VoicePipeline:
    """Construct the L5 :class:`VoicePipeline` with a fresh-conn factory.

    ADR-0005 §4.2 / §5.1 — the pipeline runs on the wake or PTT worker
    thread, never the event loop, so the conn factory MUST open a new
    SQLite connection per call (``check_same_thread`` invariant).

    A voice terminal (ADR 0172) has no log of its own to commit to: it passes ``emit``, which
    sends the utterance to the brain, and the audio it keeps stays on its own disk.

    With ``voiceprint_model`` (ADR 0286) a turn is embedded beside its final ASR; a model file
    that is not there means no voiceprint.
    """
    # realtime.final_asr_language pins SenseVoice's language, and with it the hybrid's Whisper.
    realtime = runtime.config.raw.get("realtime")
    pinned = realtime.get("final_asr_language") if isinstance(realtime, Mapping) else None
    sensevoice = voice_asr.SenseVoiceRecognizer(
        model_dir=sensevoice_dir, language=str(pinned or "") or None,
    )
    recognizer = final_recognizer(runtime, sensevoice)
    db_path = runtime.runtime_paths.event_log
    memory = runtime.memory
    artifacts_dir = memory.audio_dir if memory is not None and memory.retain_audio else None
    return voice_pipeline.VoicePipeline(
        conn_factory=None if emit is not None else lambda: open_runtime_event_log(db_path),
        emit=emit,
        recognizer=recognizer,
        broadcaster=broadcaster,
        artifacts_dir=artifacts_dir,
        cues=runtime.voice_cues,
        voiceprints=None if voiceprint_model is None else load_extractor(voiceprint_model),
    )


def build_echo_canceller(runtime: _ConfigHost) -> voice_aec.EchoCanceller | None:
    """One canceller for the streaming player and the mic ingress.

    ``echo_cancellation`` is ``auto`` (the default: on for any microphone but the
    reSpeaker, which does its own), ``true`` (always on) or ``false`` (none built).
    """
    realtime_raw = runtime.config.raw.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    ingress_raw = realtime.get("single_audio_ingress")
    ingress = ingress_raw if isinstance(ingress_raw, Mapping) else {}
    mode = ingress.get("echo_cancellation", "auto")
    if mode is False:
        return None
    # Diagnostics keep the last 8 s of mic / played / cleaned audio in memory;
    # every conversation barge-in writes them under <runtime>/aec-diagnostics.
    diagnostics = ingress.get("echo_diagnostics") is True
    return voice_aec.EchoCanceller(
        history_s=8.0 if diagnostics else 0.0, follow_device=mode is not True,
    )


def native_streaming_player(
    *,
    echo_canceller: voice_aec.EchoCanceller | None,
    **kwargs: Any,  # noqa: ANN401 - NativeAudioStreamPlayer's keyword surface
) -> voice_tts.AudioStreamPlayer | None:
    """The native (ADR 0129) player, started; ``None`` keeps her on the Python player.

    Her voice must never disappear because of this switch: a helper that cannot
    be built, or one that does not come up, logs one warning and falls back.
    The echo canceller's far end is the helper's rendered blocks (ADR 0129).
    """
    try:
        player = voice_native_out.NativeAudioStreamPlayer(
            generation_safe=True,
            playback_tap=echo_canceller.add_playback if echo_canceller is not None else None,
            **kwargs,
        )
        started = player.start()
    except Exception as exc:  # noqa: BLE001 - the switch must fail safe
        LOGGER.warning("native voice output unavailable (%r); using the Python player.", exc)
        return None
    if not started.started:
        LOGGER.warning(
            "native voice output did not start (%s); using the Python player.",
            started.reason,
        )
        return None
    return player


def tts_volume_kwargs(realtime: Mapping[str, Any]) -> dict[str, Any]:
    """MiniMax `voice_setting.vol` from ``realtime.tts_volume``.

    Absent or null keeps the MiniMaxWSClient signature default, the single place the
    calibrated value lives. Deliberately unvalidated like `output_device`, but with a wider
    blast radius: it is read before the builder's own try, so a non-numeric value raises out
    of `build_tts_pipeline` and the caller drops voice input with it, where a bad
    `output_device` only degrades TTS to text-only.
    """
    tts_volume = realtime.get("tts_volume")
    return {} if tts_volume is None else {"volume": tts_volume}


def minimax_client(
    knobs: VoiceKnobs,
    api_key: str,
    volume_kwargs: Mapping[str, Any],
    *,
    recorder: voice_artifact_store.TtsRecorder | None,
    voice_settings: VoiceSettings | None,
) -> voice_tts.MiniMaxWSClient:
    """The MiniMax client the daemon synthesizes with: the one place its knobs are applied."""
    return voice_tts.MiniMaxWSClient(
        api_key=api_key,
        voice=knobs.tts_voice,
        model=knobs.tts_model,
        primary_endpoint=knobs.tts_primary_endpoint,
        fallback_endpoint=knobs.tts_fallback_endpoint,
        sample_rate_in=knobs.tts_sample_rate_in_hz,
        connect_timeout_s=knobs.tts_connect_timeout_s,
        first_chunk_timeout_s=knobs.tts_first_chunk_timeout_s,
        between_chunk_timeout_s=knobs.tts_between_chunk_timeout_s,
        recorder=recorder,
        voice_settings=voice_settings,
        **volume_kwargs,
    )


class _EventLogPath(Protocol):
    @property
    def event_log(self) -> Path: ...


class SpeakerHost(Protocol):
    """What the speaking half of voice reads from its host: a daemon's runtime, or a terminal's.

    A voice terminal (ADR 0172) has no :class:`JarvisRuntime`; it passes the same few values.
    """

    @property
    def config(self) -> RuntimeConfig:
        """The daemon's or terminal's config."""
        ...

    @property
    def response_flags(self) -> Wave4ResponseFlags:
        """The response-wave flags."""
        ...

    @property
    def runtime_paths(self) -> _EventLogPath:
        """Where the event log lives."""
        ...

    @property
    def conn(self) -> sqlite3.Connection:
        """The state connection."""
        ...

    @property
    def memory(self) -> MemorySettings | None:
        """The memory settings, or None when memory is off."""
        ...

    @property
    def voice_settings(self) -> VoiceSettings | None:
        """The voice settings, or None."""
        ...

    @property
    def response_runs(self) -> ResponseRunRegistry | None:
        """The registry of running responses, or None."""
        ...


def build_tts_pipeline(  # noqa: PLR0913 - degradation boundary
    runtime: SpeakerHost,
    broadcaster: InherentBroadcaster,
    *,
    ducker: voice_ducking.SystemAudioDucker | None = None,
    voice: VoiceKnobs | None = None,
    echo_canceller: voice_aec.EchoCanceller | None = None,
    remote: voice_media.StreamingTTSProvider | None = None,
    network_lost_dir: Path | None = None,
) -> voice_media.StreamingTTSPipeline | None:
    """Build the speech subsystem, or ``None`` for a text-only boot.

    ``remote`` (ADR 0172) is a voice terminal's provider, whose sessions run on the brain,
    which holds the key: the media actor is built around it, with no key here.
    ``network_lost_dir`` is where a terminal keeps the audio of the cut-off line between runs,
    so a start does not pay the brain for the same sentence again.

    ADR-0005 §5.3 — the env var is the sole credential source for the MiniMax WebSocket.
    Without it we skip the whole subsystem. So does every other way the media actor cannot
    start (an invalid ``realtime.streaming_output``, the event-log features off, no TTS
    endpoint, a startup error): there is no second speech path to fall back to.

    The optional ``ducker`` is the :class:`SystemAudioDucker` the media actor takes
    its output lease from.
    """
    knobs = VoiceKnobs() if voice is None else voice
    api_key = "" if remote is not None else os.environ.get("MINIMAX_API_KEY")
    if remote is None and not api_key:
        LOGGER.warning(
            "MINIMAX_API_KEY unset; skipping TTS subsystem (text path only).",
        )
        return None
    realtime_raw = runtime.config.raw.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    volume_kwargs = tts_volume_kwargs(realtime)
    # Passed straight through to sd.OutputStream, which maps a name to a device
    # index itself. Absent from CoreAudio's list: the system default (ADR 0054).
    output_device = output_or_default(realtime.get("output_device"))
    # ADR 0052: the Settings page's voice volume, applied in the player.
    playback_volume = float(realtime.get("playback_volume") or 1.0)
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    try:
        media_config = voice_media.streaming_media_config_from_mapping(streaming)
    except ValueError as exc:
        LOGGER.warning(
            "realtime.streaming_output config invalid (%s); this boot will not speak.",
            exc,
        )
        return None
    # THE output rate, for the provider's resampler and the player.
    # ``realtime.streaming_output.canonical_sample_rate_hz`` is its only source:
    # a second constant here could only ever agree with it or silently disable
    # streaming. The default (48 kHz) is the macOS built-in device rate, so
    # CoreAudio is not forced into a hardware-rate switch on every play — which
    # was producing audible pops for any other app sharing the speaker.
    output_sample_rate_hz = media_config.canonical_sample_rate_hz

    # ADR 0120: with the diagnostics switch on, her audio is kept beside the recordings.
    recorder = (
        voice_artifact_store.TtsRecorder(runtime.memory.audio_dir)
        if remote is None
        and runtime.config.diagnostics.record_tts_audio
        and runtime.memory is not None
        else None
    )
    provider: voice_media.StreamingTTSProvider = (
        remote
        if remote is not None
        else minimax_client(
            knobs, api_key or "", volume_kwargs,
            recorder=recorder, voice_settings=runtime.voice_settings,
        )
    )
    if not (runtime.config.realtime.enabled and provider.streaming_candidate_count > 0):
        LOGGER.warning(
            "streaming speech cannot run (realtime.enabled is off, or no TTS endpoint is "
            "configured); this boot will not speak.",
        )
        return None
    player = (
        native_streaming_player(
            echo_canceller=echo_canceller,
            sample_rate_hz=output_sample_rate_hz,
            ring_seconds=media_config.ring_seconds,
            device=output_device,
            volume=playback_volume,
        )
        if streaming.get("native_player") is True
        else None
    ) or voice_tts.AudioStreamPlayer(
        sample_rate_hz=output_sample_rate_hz,
        ring_seconds=media_config.ring_seconds,
        lazy_open=True,
        generation_safe=True,
        # 2048 frames (43 ms) of slack: with 1024 the Python callback still
        # missed CoreAudio's cycle under load (~40 overloads a minute on the
        # Multi-Output device, 2026-10-02), each one a pop in her voice.
        blocksize=2048,
        # The host buffer behind it: final ASR and turn start still stall the
        # callback past 43 ms when Allen stops talking, a pop if she is speaking.
        latency=0.12,
        device=output_device,
        playback_tap=echo_canceller.add_playback if echo_canceller is not None else None,
        volume=playback_volume,
    )
    text_registry = runtime.response_runs
    try:
        return voice_media.StreamingTTSPipeline(
            provider=provider,
            player=player,
            conn_factory=lambda: open_runtime_event_log(runtime.runtime_paths.event_log),
            boot_high_water_id=latest_id(runtime.conn),
            config=(
                media_config if network_lost_dir is None
                else dataclasses.replace(media_config, network_lost_cache_dir=network_lost_dir)
            ),
            broadcaster=broadcaster,
            ducker=ducker,
            foreground_decision_callable=make_foreground_decision_callable(
                wait_for_lane=runtime.response_flags.slow_results,
            ),
            silent_intent_channels=TTS_SILENT_CHANNELS,
            speaks_turn=(
                None if text_registry is None
                else lambda _conn, turn_id: not text_registry.turn_text_only(turn_id)
            ),
        )
    except voice_media.StreamingMediaStartupError as exc:
        LOGGER.warning(
            "realtime.streaming_output startup failed in %s (output device %s); "
            "this boot is text-only.",
            exc.phase,
            exc.device_disposition,
        )
    except Exception as exc:  # noqa: BLE001 - the speech subsystem fails to text-only
        LOGGER.warning(
            "realtime.streaming_output startup failed (%r); this boot is text-only.",
            exc,
        )
    if isinstance(player, voice_native_out.NativeAudioStreamPlayer):
        player.stop()
    return None


def build_voice_pipeline_callable(
    pipeline: voice_pipeline.VoicePipeline,
) -> Callable[[bytes, str, str, str], Event]:
    """Adapt :meth:`VoicePipeline.run_turn` to the PTT callable shape.

    ``_submit_asr_v2`` calls it with positional
    ``(audio_bytes, turn_id, channel, language)`` and gets back the
    emitted ``utterance.received`` :class:`Event`; the pipeline
    itself is keyword-only, so this thin closure does the rewrite.

    The closure forces ``broadcast=False`` — this callable is the PTT
    path (``/inherent/asr-submit/v2``), and per ADR-0005 §6 the client
    drives its state from the HTTP response body, not from
    WS ``op:voice`` envelopes. The shared :class:`VoicePipeline`
    instance keeps its broadcaster wired for the wake path; this
    adapter just silences phase envelopes for PTT.
    """

    def _call(audio_bytes: bytes, turn_id: str, channel: str, language: str) -> Event:
        return pipeline.run_turn(
            audio_bytes=audio_bytes,
            turn_id=turn_id,
            channel=channel,
            language=language,
            broadcast=False,
        )

    return _call


class _RuntimeRoot(Protocol):
    @property
    def root(self) -> Path: ...


class ListeningHost(_ConfigHost, Protocol):
    """What the capture session is built from: a daemon's runtime, or a voice terminal's seat."""

    @property
    def runtime_paths(self) -> _RuntimeRoot:
        """Where the capture session finds its runtime root."""
        ...


@dataclasses.dataclass(frozen=True)
class ListenPorts:
    """What the capture session reaches in the turn's state, and how (ADR 0172).

    On one machine every port is a direct call into the runtime
    (:func:`~jarvis.runtime.daemon.listen_ports.local_listen_ports`).
    On a voice terminal each is a call over the brain link, or absent. The local halves of
    hold-output, stop-speaking and the like (the player, the input model) stay in
    :func:`_spawn_single_ingress_session`, which runs wherever the microphone is.
    """

    answer_words: Callable[[str, str, str], None] | None = None
    turn_working: Callable[[], bool] | None = None
    recent_speech: Callable[[], str] | None = None
    ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None
    note_words: Callable[[str, str, str, bool, bool], None] | None = None
    begin_line: Callable[[str, str, str, bool, bool], None] | None = None
    interrupt: Callable[[str], str] | None = None
    """Cancel the answer still being written (the generation half of stop-speaking)."""
    hold_runs: Callable[[bool], None] | None = None
    """The run-hold half of hold-output."""
    warm: Callable[[], None] | None = None
    """Open the model connection a request is about to need."""
    supersede_unspoken: Callable[[str], None] | None = None
    cancel_voice_runs: Callable[[], None] | None = None
    heard_elsewhere: Callable[[str, str], bool] | None = None
    """``(turn_id, text) -> bool``: another device recorded these words just now (ADR 0219)."""
    voiceprints: VoiceprintBook | None = None
    """The owner's profiles, for a session that judges its own words (ADR 0286)."""
    judged_on_send: bool = False
    """The brain judges the words as it receives them, so the ports above that only the judge
    reaches (``ask_words``, ``note_words``, ``begin_line``, ``recent_speech``,
    ``heard_elsewhere``, ``supersede_unspoken``) are the brain's own (ADR 0277)."""
    cues: VoiceCues | None = None
    ambient: AmbientSounds | None = None


def single_ingress_activation(
    runtime: _ConfigHost,
    *,
    tts: voice_media.StreamingTTSPipeline | None,
) -> SingleIngressActivation:
    """Validate feature flags, Wave-1/2 dependencies, and strict input bounds."""
    realtime_raw = runtime.config.raw.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    ingress_raw = realtime.get("single_audio_ingress")
    ingress_values = ingress_raw if isinstance(ingress_raw, Mapping) else {}
    requested = ingress_values.get("enabled") is True
    if not requested:
        return SingleIngressActivation(
            requested=False,
            capable=False,
            reason="feature_disabled",
        )
    if not runtime.config.realtime.enabled:
        return SingleIngressActivation(
            requested=True,
            capable=False,
            reason="realtime_parent_disabled",
        )
    if ingress_values.get("backend", "sounddevice") != "sounddevice":
        return SingleIngressActivation(
            requested=True,
            capable=False,
            reason="unsupported_input_backend",
        )
    try:
        ingress_config = voice_audio.audio_ingress_config_from_mapping(ingress_values)
        session_config = voice_session.realtime_input_session_config_from_mapping(
            ingress_values,
        )
    except ValueError as exc:
        return SingleIngressActivation(
            requested=True,
            capable=False,
            reason=f"invalid_input_config:{exc}",
        )
    if tts is None:
        return SingleIngressActivation(
            requested=True,
            capable=False,
            reason="streaming_output_missing",
        )
    return SingleIngressActivation(
        requested=True,
        capable=True,
        reason="validated",
        ingress_config=ingress_config,
        session_config=session_config,
    )
