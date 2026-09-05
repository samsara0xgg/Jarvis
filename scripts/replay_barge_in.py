"""Replay a wake+keyword WAV into a speaking Jarvis (ADR-0006 D8 live check).

The composition root of ``scripts/replay_endpointing.py`` plus the three
things a barge-in needs to be real: the actual OpenWakeWord engine, a real
``StreamingTTSPipeline`` playing a real answer, and a real ``ResponseRun``
registered in the runtime's live-run index.  The WAV — the wake phrase
followed by an interrupt keyword — is fed by ``FileReplayBackend`` at
real-time pace while that answer is speaking, so the wake hit lands during
active output exactly as it would from a microphone.

Nothing here is a fixture: the cancel goes through
``make_barge_in_interrupt_callable`` into the ordinary generation-scope cancel
path, and the durable rows printed at the end are the ones the daemon writes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import yaml

from jarvis.decision.response_run import legacy_full_text_policy, start_response_run
from jarvis.runtime import bootstrap_runtime_app, make_barge_in_interrupt_callable
from jarvis.runtime.inherent_loop import _build_tts_pipeline, _fetch_events_after
from jarvis.shared.realtime import new_response_id, stable_response_group_id
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_media,
    voice_pipeline,
    voice_session,
    voice_wake,
)
from jarvis.surface.inherent_output import InherentBroadcaster

_SPEAKER_FALLBACK = "MacBook Pro Speakers"
_TRAIL_NAMES = frozenset(
    {
        "barge_in_candidate",
        "barge_in_confirmed",
        "barge_in_candidate_dropped",
        "audio_input_wake_detected",
        "audio_input_wake_suppressed",
        "audio_input_capture_started",
        "asr_partial",
        "endpoint_decision",
        "audio_input_endpoint_committed",
        "response_cancel_requested",
    },
)
# The spoken answer only has to be long enough to still be playing when the
# wake phrase lands. Fullwidth commas are escaped so ruff's ambiguous-unicode
# rule stays on for the rest of the file.
_ANSWER = (
    "好的\uff0c我来讲一个比较长的故事。从前有一座山\uff0c山里有一座庙\uff0c"
    "庙里有一个老和尚在给小和尚讲故事。这个故事很长\uff0c它会一直讲下去\uff0c"
    "直到有人开口把它打断为止。"
)


def _wav_duration_s(path: Path) -> float:
    with contextlib.closing(wave.open(str(path), "rb")) as handle:
        return handle.getnframes() / float(handle.getframerate())


def _current_output_device() -> str | None:
    try:
        result = subprocess.run(
            ["SwitchAudioSource", "-c", "-t", "output"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _set_output_device(name: str) -> bool:
    try:
        subprocess.run(  # noqa: S603 - fixed argv, never shell
            ["SwitchAudioSource", "-s", name, "-t", "output"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _print_trail() -> None:
    for point in realtime_trace_snapshot():
        if point.name not in _TRAIL_NAMES:
            continue
        attributes = {
            key: value
            for key, value in point.attributes.items()
            if key not in {"session_id", "measurement_boundary"}
        }
        sys.stdout.write(f"  {point.monotonic_ns} {point.name} {attributes}\n")


def _elapsed_candidate_to_confirm_ms() -> float | None:
    points = {
        point.name: point.monotonic_ns
        for point in realtime_trace_snapshot()
        if point.name in {"barge_in_candidate", "barge_in_confirmed"}
    }
    if "barge_in_candidate" not in points or "barge_in_confirmed" not in points:
        return None
    return (points["barge_in_confirmed"] - points["barge_in_candidate"]) / 1_000_000


def _print_rows(db_path: Path, since_id: int) -> None:
    with contextlib.closing(open_event_log(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, type, payload_json FROM events WHERE id > ? AND type IN"
            " ('response.cancelled', 'response.completed', 'response.failed',"
            " 'surface.playback_interrupted', 'surface.playback_segment_prepared',"
            " 'surface.playback_started', 'utterance.received') ORDER BY id",
            (since_id,),
        ).fetchall()
    for row_id, row_type, payload_json in rows:
        payload = json.loads(payload_json)
        sys.stdout.write(f"  #{row_id} {row_type} {payload}\n")


def _last_event_id(db_path: Path) -> int:
    with contextlib.closing(open_event_log(db_path)) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0]) if row else 0


_MEDIA_ROW_TYPES = (
    "surface.response_open",
    "surface.response_chunk",
    "surface.response_emitted",
    "response.cancelled",
    "response.failed",
)


def _media_pump(
    pipeline: voice_media.StreamingTTSPipeline,
    db_path: Path,
    after_id: int,
    stop: threading.Event,
) -> None:
    """Stand in for ``_tts_watcher``: durable rows into the media owner.

    The script cannot start the daemon's watcher, and the media owner is
    fed only by that cursor — including ``response.cancelled``, which is
    what makes the cancel stop the speech.
    """

    async def _run() -> None:
        conn = open_event_log(db_path)
        cursor = after_id
        try:
            while not stop.is_set():
                for row_id, event in _fetch_events_after(
                    conn,
                    after_id=cursor,
                    event_types=_MEDIA_ROW_TYPES,
                ):
                    outcome = await pipeline.submit_event(
                        row_id=row_id, event=event, origin="replay",
                    )
                    sys.stdout.write(
                        f"  pump #{row_id} {event.type} -> {outcome.status}\n",
                    )
                    cursor = max(cursor, row_id)
                await asyncio.sleep(0.02)
        finally:
            conn.close()

    asyncio.run(_run())


def _replay(  # noqa: PLR0913, PLR0915 - one composition root, printed end to end
    wav: Path,
    *,
    config_path: Path,
    runtime_root: Path,
    sensevoice_dir: Path,
    silero: Path,
    speak_lead_s: float,
    tail_s: float,
) -> int:
    reset_realtime_trace()
    runtime = bootstrap_runtime_app(
        config_path=config_path,
        prompt_path=Path("prompts/jarvis_v1.md"),
        runtime_root=runtime_root,
    )
    db_path = runtime.runtime_paths.event_log
    since_id = _last_event_id(db_path)
    values = yaml.safe_load(config_path.read_text())["realtime"]["single_audio_ingress"]

    tts = _build_tts_pipeline(runtime, InherentBroadcaster())
    if tts is None:
        sys.stdout.write("  no TTS pipeline (MINIMAX_API_KEY unset?); cannot speak\n")
        return 2

    factory = runtime.llm_session_factory
    registry = runtime.response_runs
    if factory is None or registry is None:
        sys.stdout.write("  runtime has no ResponseRun lifecycle/cancel wiring\n")
        return 2

    turn_id = "T" + os.urandom(4).hex()
    trigger = emit_event(
        runtime.conn,
        type="utterance.received",
        payload={"transcript": "讲个长故事", "turn_id": turn_id},
    )
    response_id = new_response_id()
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=trigger.event_uid,
        request_client=factory.create(factory.snapshot(), response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash="0" * 64,
            preset_snapshot_hash="0" * 64,
        ),
        response_id=response_id,
    )
    registry.register(run)
    sys.stdout.write(
        f"  response_id={response_id} confirmed_playback="
        f"{run.interrupt_policy.confirmed_playback}\n",
    )

    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=voice_asr.SenseVoiceRecognizer(model_dir=sensevoice_dir),
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=runtime_root / "voice_artifacts",
    )
    pipeline.prewarm_input_model()
    ingress = voice_audio.AudioIngress(
        backend=voice_backend.FileReplayBackend(wav, tail_silence_s=None),
        config=voice_audio.audio_ingress_config_from_mapping(values),
    )
    engine = voice_wake.WakeEngine(model_name="hey_jarvis_v0.1")
    engine.start()
    session = voice_session.DuplexVoiceSession(
        ingress=ingress,
        wake_engine=engine,
        vad=voice_audio.SileroVad(mode="record", model_path=silero),
        pipeline=pipeline,
        broadcaster=None,
        output_active=tts.is_output_active,
        wake_threshold=0.5,
        config=voice_session.realtime_input_session_config_from_mapping(values),
        barge_in_interrupt=make_barge_in_interrupt_callable(runtime),
    )

    # Start speaking first: the wake hit has to land during ACTIVE output.
    if not isinstance(tts, voice_media.StreamingTTSPipeline):
        sys.stdout.write("  TTS downgraded to the legacy pipeline; no streaming owner\n")
        return 2
    group_id = stable_response_group_id(turn_id)
    stop = threading.Event()
    pump = threading.Thread(
        target=_media_pump,
        args=(tts, db_path, since_id, stop),
        name="replay-media-pump",
        daemon=True,
    )
    pump.start()
    for row_type, payload in (
        ("surface.response_open", {"turn_id": turn_id, "required_gate_mode": "sentence",
                                   "response_id": response_id, "channel": "both",
                                   "response_group_id": group_id,
                                   "query": "讲个长故事", "kind": "answer"}),
        ("surface.response_chunk", {"turn_id": turn_id, "text": _ANSWER,
                                    "response_id": response_id, "sequence": 0,
                                    "response_group_id": group_id}),
        ("surface.response_emitted", {"turn_id": turn_id, "response_id": response_id,
                                      "response_group_id": group_id, "text": _ANSWER}),
    ):
        emit_event(runtime.conn, type=row_type, payload=payload)
    deadline = time.monotonic() + speak_lead_s
    while time.monotonic() < deadline and not tts.is_output_active():
        time.sleep(0.02)
    sys.stdout.write(
        f"  output_active_before_replay={tts.is_output_active()} "
        f"speaking={tts.is_speaking()}\n",
    )

    started = session.start()
    if not started.started:
        sys.stdout.write(f"  session start failed: {started.ingress.capability.reason}\n")
        return 2
    time.sleep(_wav_duration_s(wav) + tail_s)
    metrics = session.metrics()
    close = session.close()
    stop.set()
    pump.join(timeout=2.0)
    tts.close(wait_timeout_s=5.0)

    sys.stdout.write("== trail\n")
    _print_trail()
    sys.stdout.write("== durable rows\n")
    _print_rows(db_path, since_id)
    elapsed = _elapsed_candidate_to_confirm_ms()
    sys.stdout.write(
        f"candidates={metrics.barge_in_candidates} "
        f"dropped={metrics.barge_in_candidates_dropped} "
        f"confirmed={metrics.barge_in_confirmations} "
        f"suppressed={metrics.wake_suppressed_during_output} "
        f"partial_decodes={metrics.partial_decodes} "
        f"candidate_to_confirm_ms={elapsed} "
        f"closed={close.definitively_closed}\n",
    )
    runtime.conn.close()
    return 0 if metrics.barge_in_confirmations else 1


def main(argv: list[str] | None = None) -> int:
    """Switch output to the loopback device, replay once, restore the route."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--sensevoice-dir", type=Path, default=Path("data/sensevoice-small-int8"))
    parser.add_argument("--silero", type=Path, default=Path("data/silero_vad.onnx"))
    parser.add_argument("--output-device", default="BlackHole 16ch")
    parser.add_argument("--speak-lead-s", type=float, default=8.0)
    parser.add_argument("--tail-s", type=float, default=6.0)
    args = parser.parse_args(argv)

    previous = _current_output_device()
    sys.stdout.write(f"SwitchAudioSource -c -t output (before): {previous}\n")
    if previous == args.output_device:
        # Another lane's guard is mid-run. Restoring the loopback device would
        # leave this machine with no audible output, so restore to the speakers.
        previous = _SPEAKER_FALLBACK
        sys.stdout.write(f"  pre-run device is the loopback; will restore {previous!r}\n")
    if previous is None:
        sys.stdout.write("  SwitchAudioSource unavailable; not switching the route\n")
    elif not _set_output_device(args.output_device):
        sys.stdout.write(f"  could not select {args.output_device!r}; keeping {previous!r}\n")
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-barge-in-") as tmp:
            root = args.runtime_root if args.runtime_root is not None else Path(tmp)
            return _replay(
                args.wav,
                config_path=args.config,
                runtime_root=root,
                sensevoice_dir=args.sensevoice_dir,
                silero=args.silero,
                speak_lead_s=args.speak_lead_s,
                tail_s=args.tail_s,
            )
    finally:
        if previous is not None:
            _set_output_device(previous)
        sys.stdout.write(
            f"SwitchAudioSource -c -t output (after): {_current_output_device()}\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
