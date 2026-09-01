"""Isolated real-provider Wave 2 latency A/B and optional full system smoke."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import statistics
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import yaml

import jarvis
from jarvis.runtime import bootstrap_runtime_app, drive_turn
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.cli import emit_surface_user_intent
from jarvis.surface.sentence_splitter import split_into_sentences
from jarvis.surface.voice_media import (
    StreamingMediaConfig,
    StreamingTTSPipeline,
    streaming_media_config_from_mapping,
)
from jarvis.surface.voice_tts import (
    AudioStreamPlayer,
    MiniMaxWSClient,
    TTSAudioChunk,
    TTSPipeline,
    TTSResponseSegment,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable

    from jarvis.shared import Event

_SAMPLE_RATE_HZ = 48_000


@dataclass(frozen=True)
class RunMetrics:
    """One raw monotonic latency sample; every field is milliseconds."""

    mode: Literal["legacy_batch", "streaming_output", "system_smoke"]
    request_to_session_open_ms: float | None
    request_to_provider_first_pcm_ms: float
    request_to_player_accept_ms: float
    request_to_first_callback_ms: float
    request_to_provider_final_ms: float
    request_to_software_drain_ms: float
    request_to_estimated_audible_ms: float | None
    player_accept_minus_provider_final_ms: float
    measurement_note: str


def _relative_ms(points: dict[str, int], start: str, end: str) -> float:
    return (points[end] - points[start]) / 1_000_000


def _first_points(names: Iterable[str]) -> dict[str, int]:
    wanted = set(names)
    found: dict[str, int] = {}
    for point in realtime_trace_snapshot():
        if point.name in wanted and point.name not in found:
            found[point.name] = point.monotonic_ns
    missing = wanted - found.keys()
    if missing:
        msg = f"missing realtime trace milestones: {sorted(missing)}"
        raise RuntimeError(msg)
    return found


def _legacy_run(*, api_key: str, text: str) -> RunMetrics:
    reset_realtime_trace()
    provider = MiniMaxWSClient(
        api_key=api_key,
        sample_rate_in=32_000,
        sample_rate_out=_SAMPLE_RATE_HZ,
    )
    player = AudioStreamPlayer(
        sample_rate_hz=_SAMPLE_RATE_HZ,
        ring_seconds=30.0,
        lazy_open=False,
    )
    pipeline = TTSPipeline(
        provider=provider,
        player=player,
        fallback=None,
    )
    try:
        pipeline.begin_turn("BENCH-LEGACY", gate_mode="full_text")
        pipeline.handle_chunk("BENCH-LEGACY", text)
        pipeline.handle_emitted("BENCH-LEGACY")
        if not pipeline.wait_until_idle(timeout_s=60.0):
            msg = "legacy batch pipeline did not drain"
            raise TimeoutError(msg)
        # The callback only publishes a lock-free diagnostic flag; the owner
        # must flush it before the audit reads trace milestones.
        player.poll_presentation()
        points = _first_points(
            (
                "tts_batch_synthesis_started",
                "tts_provider_first_pcm_received",
                "tts_batch_synthesis_completed",
                "tts_first_pcm_accepted_to_ring",
                "audio_output_first_nonzero_callback",
                "audio_ring_empty_observed",
            ),
        )
    finally:
        pipeline.close(wait_timeout_s=3.0)
    start = "tts_batch_synthesis_started"
    provider_final = "tts_batch_synthesis_completed"
    accept = "tts_first_pcm_accepted_to_ring"
    return RunMetrics(
        mode="legacy_batch",
        request_to_session_open_ms=None,
        request_to_provider_first_pcm_ms=_relative_ms(
            points,
            start,
            "tts_provider_first_pcm_received",
        ),
        request_to_player_accept_ms=_relative_ms(points, start, accept),
        request_to_first_callback_ms=_relative_ms(
            points,
            start,
            "audio_output_first_nonzero_callback",
        ),
        request_to_provider_final_ms=_relative_ms(points, start, provider_final),
        request_to_software_drain_ms=_relative_ms(
            points,
            start,
            "audio_ring_empty_observed",
        ),
        request_to_estimated_audible_ms=None,
        player_accept_minus_provider_final_ms=_relative_ms(
            points,
            provider_final,
            accept,
        ),
        measurement_note=(
            "first callback and software ring empty are software boundaries; "
            "legacy has no conservative DAC horizon"
        ),
    )


def _emit_bench_events(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    group_id = "RGRP-" + response_id
    chunks = split_into_sentences(text) or [text]
    events = [
        emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": turn_id,
                "query": "latency benchmark",
                "kind": "text",
                "response_id": response_id,
                "response_group_id": group_id,
                "phase": "final",
                "channel": "speech",
            },
        ),
        *[
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": turn_id,
                    "text": chunk,
                    "response_id": response_id,
                    "response_group_id": group_id,
                    "sequence": sequence,
                    "phase": "final",
                    "channel": "speech",
                },
            )
            for sequence, chunk in enumerate(chunks)
        ],
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": turn_id,
                "text": text,
                "response_id": response_id,
                "response_group_id": group_id,
                "phase": "final",
                "channel": "speech",
            },
        ),
    ]
    rows: list[tuple[int, Event]] = []
    for event in events:
        row = conn.execute(
            "SELECT id FROM events WHERE event_uid = ?",
            (event.event_uid,),
        ).fetchone()
        if row is None:
            msg = f"benchmark event disappeared: {event.event_uid}"
            raise RuntimeError(msg)
        rows.append((int(row[0]), event))
    return rows


async def _submit_all(
    pipeline: StreamingTTSPipeline,
    rows: Iterable[tuple[int, Event]],
) -> None:
    for row_id, event in rows:
        outcome = await pipeline.submit_event(
            row_id=row_id,
            event=event,
            origin="benchmark",
        )
        if outcome.status != "accepted":
            msg = f"benchmark media command rejected: {outcome}"
            raise RuntimeError(msg)


def _streaming_run(
    *,
    api_key: str,
    text: str,
    workspace: Path,
    media_config: StreamingMediaConfig,
) -> RunMetrics:
    reset_realtime_trace()
    workspace.mkdir(parents=True)
    db_path = workspace / "streaming-events.db"
    conn = open_event_log(db_path)
    provider = MiniMaxWSClient(
        api_key=api_key,
        sample_rate_in=32_000,
        sample_rate_out=_SAMPLE_RATE_HZ,
    )
    player = AudioStreamPlayer(
        sample_rate_hz=_SAMPLE_RATE_HZ,
        ring_seconds=2.0,
        lazy_open=True,
        generation_safe=True,
    )
    pipeline = StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=media_config,
    )
    try:
        rows = _emit_bench_events(
            conn,
            response_id=f"RESP-BENCH-{time.monotonic_ns()}",
            turn_id="BENCH-STREAMING",
            text=text,
        )
        asyncio.run(_submit_all(pipeline, rows))
        if not pipeline.wait_until_idle(timeout_s=60.0):
            msg = "streaming pipeline did not drain"
            raise TimeoutError(msg)
        points = _first_points(
            (
                "tts_request_started",
                "tts_session_opened",
                "tts_provider_first_pcm_received",
                "tts_player_first_accept",
                "audio_output_first_nonzero_callback",
                "tts_provider_final",
                "tts_software_drain",
                "tts_estimated_audible",
            ),
        )
    finally:
        pipeline.close(wait_timeout_s=3.0)
        conn.close()
    start = "tts_request_started"
    provider_final = "tts_provider_final"
    accept = "tts_player_first_accept"
    return RunMetrics(
        mode="streaming_output",
        request_to_session_open_ms=_relative_ms(points, start, "tts_session_opened"),
        request_to_provider_first_pcm_ms=_relative_ms(
            points,
            start,
            "tts_provider_first_pcm_received",
        ),
        request_to_player_accept_ms=_relative_ms(points, start, accept),
        request_to_first_callback_ms=_relative_ms(
            points,
            start,
            "audio_output_first_nonzero_callback",
        ),
        request_to_provider_final_ms=_relative_ms(points, start, provider_final),
        request_to_software_drain_ms=_relative_ms(
            points,
            start,
            "tts_software_drain",
        ),
        request_to_estimated_audible_ms=_relative_ms(
            points,
            start,
            "tts_estimated_audible",
        ),
        player_accept_minus_provider_final_ms=_relative_ms(
            points,
            provider_final,
            accept,
        ),
        measurement_note=(
            "first callback is host submission; estimated audible is a conservative "
            "software estimate, never measured DAC or loopback"
        ),
    )


async def _first_streaming_pcm(
    *,
    provider: MiniMaxWSClient,
    text: str,
    playback_generation_id: int,
) -> tuple[TTSAudioChunk, int, int]:
    """Open the production streaming session and retain its first PCM timing."""
    session = provider.create_tts_session(
        endpoint_index=0,
        idle_close_s=10.0,
        command_queue_capacity=2,
        audio_queue_capacity=16,
    )
    opened_ns = time.monotonic_ns()
    try:
        async with asyncio.timeout(15.0):
            await session.open("BENCH-BOUNDED-SMOKE", playback_generation_id)
            opened_ns = time.monotonic_ns()
            await session.send(
                TTSResponseSegment(
                    response_id="BENCH-BOUNDED-SMOKE",
                    playback_generation_id=playback_generation_id,
                    sequence=0,
                    text=text,
                ),
            )
            async for event in session.audio_events():
                if isinstance(event, TTSAudioChunk) and event.pcm:
                    return event, opened_ns, time.monotonic_ns()
        msg = "provider stream ended before first PCM"
        raise RuntimeError(msg)
    finally:
        with contextlib.suppress(Exception):
            await session.abort("bounded_smoke_complete")
        with contextlib.suppress(Exception):
            await session.close()


def _bounded_real_streaming_smoke(
    *,
    api_key: str,
    text: str,
) -> dict[str, object]:
    """Probe real MiniMax first PCM through the real PortAudio callback boundary."""
    reset_realtime_trace()
    request_ns = time.monotonic_ns()
    provider = MiniMaxWSClient(
        api_key=api_key,
        sample_rate_in=32_000,
        sample_rate_out=_SAMPLE_RATE_HZ,
    )
    player = AudioStreamPlayer(
        sample_rate_hz=_SAMPLE_RATE_HZ,
        ring_seconds=2.0,
        lazy_open=False,
        generation_safe=True,
    )
    lease = player.activate_generation(
        session_id="BENCH-BOUNDED-SMOKE",
        response_id="BENCH-BOUNDED-SMOKE",
        response_group_id="BENCH-BOUNDED-SMOKE",
        turn_id="BENCH-BOUNDED-SMOKE",
    )
    result: dict[str, object] = {
        "status": "FAIL",
        "measurement_semantics": (
            "real provider first PCM and software ring accept; PortAudio callback "
            "is host submission, never physical DAC/loopback"
        ),
    }
    try:
        generation = getattr(lease, "playback_generation_id", None)
        if not isinstance(generation, int):
            result["failure"] = "generation_player_refused_bounded_smoke_lease"
            return result
        player.begin_generation_segment(
            expected_playback_generation_id=generation,
            sequence=0,
            text=text,
            segment_hash=hashlib.sha256(text.encode()).hexdigest(),
        )
        chunk, opened_ns, first_pcm_ns = asyncio.run(
            _first_streaming_pcm(
                provider=provider,
                text=text,
                playback_generation_id=generation,
            ),
        )
        samples = np.frombuffer(chunk.pcm, dtype="<i2").astype(np.float32)
        samples /= 32768.0
        if chunk.sample_rate_hz != _SAMPLE_RATE_HZ:
            import soxr  # noqa: PLC0415

            samples = np.asarray(
                soxr.resample(samples, chunk.sample_rate_hz, _SAMPLE_RATE_HZ),
                dtype=np.float32,
            )
        accepted = player.write_generation(
            samples.tobytes(),
            expected_playback_generation_id=generation,
            segment_sequence=0,
        )
        accepted_ns = time.monotonic_ns()
        accepted_count = getattr(accepted, "sample_count", 0)
        result.update(
            {
                "request_to_session_open_ms": (opened_ns - request_ns) / 1_000_000,
                "request_to_provider_first_pcm_ms": (first_pcm_ns - request_ns) / 1_000_000,
                "request_to_player_accept_ms": (accepted_ns - request_ns) / 1_000_000,
                "provider_pcm_bytes": len(chunk.pcm),
                "player_accepted_samples": int(accepted_count),
            },
        )
        callback_deadline = time.monotonic() + 2.0
        callback_ns: int | None = None
        while time.monotonic() < callback_deadline:
            player.poll_presentation()
            callback_point = next(
                (
                    point
                    for point in realtime_trace_snapshot()
                    if point.name == "audio_output_first_nonzero_callback"
                ),
                None,
            )
            if callback_point is not None:
                callback_ns = callback_point.monotonic_ns
                break
            time.sleep(0.005)
        if callback_ns is None:
            result["failure"] = "portaudio_active_but_no_nonzero_callback_within_2s"
        else:
            result["status"] = "PASS"
            result["request_to_first_callback_ms"] = (
                callback_ns - request_ns
            ) / 1_000_000
    except Exception as exc:  # noqa: BLE001 - smoke must persist its failure artifact
        result["failure"] = f"{type(exc).__name__}:{exc}"
    finally:
        request_close = getattr(provider, "request_close", None)
        if callable(request_close):
            with contextlib.suppress(Exception):
                request_close()
        player.stop()
    return result


def _summary(
    runs: list[RunMetrics],
    *,
    eligibility_reasons: list[str],
) -> dict[str, object]:
    by_mode: dict[str, list[RunMetrics]] = {}
    for run in runs:
        by_mode.setdefault(run.mode, []).append(run)
    summary: dict[str, object] = {}
    for mode, samples in by_mode.items():
        accepts = [sample.request_to_player_accept_ms for sample in samples]
        summary[mode] = {
            "player_accept_ms_median": statistics.median(accepts),
            "player_accept_ms_range": [min(accepts), max(accepts)],
            "accept_minus_provider_final_ms": [
                sample.player_accept_minus_provider_final_ms for sample in samples
            ],
        }
    legacy = by_mode.get("legacy_batch", [])
    streaming = by_mode.get("streaming_output", [])
    software_gate = (
        bool(legacy and streaming)
        and all(sample.player_accept_minus_provider_final_ms < 0 for sample in streaming)
        and statistics.median(sample.request_to_player_accept_ms for sample in streaming)
        < statistics.median(sample.request_to_player_accept_ms for sample in legacy)
    )
    if eligibility_reasons:
        summary["software_streaming_output_gate"] = "INELIGIBLE"
    else:
        summary["software_streaming_output_gate"] = "PASS" if software_gate else "FAIL"
    summary["physical_dac_loopback_gate"] = "UNMEASURED"
    summary["true_end_to_end_latency_gate"] = "UNMEASURED"
    return summary


def _provenance(
    *,
    config_path: Path,
    text: str,
    expected_revision: str,
    media_config: StreamingMediaConfig,
) -> tuple[dict[str, object], list[str]]:
    """Capture reproducible, non-sensitive execution provenance."""
    repository = Path(__file__).resolve().parents[1]

    def _git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed local git executable/arguments
            ("/usr/bin/git", "-C", str(repository), *args),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    try:
        revision = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain=v1"))
    except (OSError, subprocess.CalledProcessError):
        revision = "unknown"
        dirty = None
    resolved_config = config_path.resolve()
    config_hash = (
        hashlib.sha256(resolved_config.read_bytes()).hexdigest()
        if resolved_config.is_file()
        else None
    )
    try:
        import sounddevice as sd  # noqa: PLC0415

        output = sd.query_devices(kind="output")
        device: dict[str, object] = {
            "name": str(output.get("name", "unknown")),
            "hostapi": int(output.get("hostapi", -1)),
            "max_output_channels": int(output.get("max_output_channels", 0)),
            "default_samplerate_hz": float(output.get("default_samplerate", 0.0)),
        }
    except Exception as exc:  # noqa: BLE001 - provenance must not mask a run result
        device = {"unavailable": type(exc).__name__}
    module_path = Path(jarvis.__file__).resolve()
    try:
        git_root = Path(_git("rev-parse", "--show-toplevel")).resolve()
    except (OSError, subprocess.CalledProcessError):
        git_root = repository
    eligibility_reasons: list[str] = []
    if revision == "unknown" or revision != expected_revision:
        eligibility_reasons.append("git_revision_mismatch")
    if dirty is not False:
        eligibility_reasons.append("git_worktree_not_clean")
    if git_root != repository or not module_path.is_relative_to(repository):
        eligibility_reasons.append("jarvis_module_outside_benchmark_worktree")
    expected_config_path = repository / "config" / "jarvis.yaml"
    if resolved_config != expected_config_path or not resolved_config.is_file():
        eligibility_reasons.append("effective_config_source_mismatch")
    effective_config = asdict(media_config)
    effective_config_hash = hashlib.sha256(
        json.dumps(effective_config, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return {
        "git_revision": revision,
        "expected_git_revision": expected_revision,
        "git_dirty": dirty,
        "repository_root": str(repository),
        "git_toplevel": str(git_root),
        "jarvis_module_path": str(module_path),
        "config_path": str(resolved_config),
        "config_sha256": config_hash,
        "effective_streaming_config": effective_config,
        "effective_streaming_config_sha256": effective_config_hash,
        "test_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "software_gate_eligible": not eligibility_reasons,
        "software_gate_ineligibility_reasons": eligibility_reasons,
        "provider": {
            "name": "MiniMax",
            "transport": "websocket_pcm_stream",
            "model": "speech-2.8-turbo",
            "input_sample_rate_hz": 32_000,
            "canonical_output_sample_rate_hz": _SAMPLE_RATE_HZ,
        },
        "output_device": device,
        "measurement": {
            "clock": "time.monotonic_ns",
            "player_accept": "software generation ring acceptance",
            "first_callback": "first nonzero PortAudio host callback",
            "estimated_audible": "software callback plus configured output-latency horizon",
            "physical_dac_or_loopback": False,
            "full_input_to_audible_e2e": False,
        },
    }, eligibility_reasons


def _load_media_config(config_path: Path) -> StreamingMediaConfig:
    """Load the exact streaming config whose path/hash enters provenance."""
    payload = yaml.safe_load(config_path.read_text())
    if not isinstance(payload, Mapping):
        msg = "benchmark config root must be a mapping"
        raise TypeError(msg)
    realtime_raw = payload.get("realtime")
    realtime = realtime_raw if isinstance(realtime_raw, Mapping) else {}
    streaming_raw = realtime.get("streaming_output")
    streaming = streaming_raw if isinstance(streaming_raw, Mapping) else {}
    return streaming_media_config_from_mapping(streaming)


def _system_smoke(
    *,
    api_key: str,
    utterance: str,
    config_path: Path,
    workspace: Path,
    media_config: StreamingMediaConfig,
) -> RunMetrics:
    reset_realtime_trace()
    runtime_root = workspace / "system-runtime"
    runtime = bootstrap_runtime_app(
        config_path=config_path,
        runtime_root=runtime_root,
    )
    provider = MiniMaxWSClient(
        api_key=api_key,
        sample_rate_in=32_000,
        sample_rate_out=_SAMPLE_RATE_HZ,
    )
    player = AudioStreamPlayer(
        sample_rate_hz=_SAMPLE_RATE_HZ,
        ring_seconds=2.0,
        lazy_open=True,
        generation_safe=True,
    )
    high_water = int(runtime.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    pipeline = StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(runtime.runtime_paths.event_log),
        boot_high_water_id=high_water,
        config=media_config,
    )
    turn_id = "SYSTEM-SMOKE"
    try:
        intent = emit_surface_user_intent(
            runtime.conn,
            transcript=utterance,
            turn_id=turn_id,
        )
        llm_start_ns = time.monotonic_ns()
        drive_turn(
            runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        llm_end_ns = time.monotonic_ns()
        rows_raw = runtime.conn.execute(
            "SELECT id, event_uid, type, schema_version, ts_epoch_ms, payload_json, "
            "source_event_id, correlation_json FROM events WHERE id > ? AND type IN "
            "('surface.response_open', 'surface.response_chunk', "
            "'surface.response_emitted') ORDER BY id",
            (high_water,),
        ).fetchall()
        from jarvis.runtime.inherent_loop import _row_to_id_event  # noqa: PLC0415

        rows = [_row_to_id_event(tuple(row)) for row in rows_raw]
        asyncio.run(_submit_all(pipeline, rows))
        if not pipeline.wait_until_idle(timeout_s=60.0):
            msg = "full system smoke streaming output did not drain"
            raise TimeoutError(msg)
        points = _first_points(
            (
                "tts_request_started",
                "tts_session_opened",
                "tts_provider_first_pcm_received",
                "tts_player_first_accept",
                "audio_output_first_nonzero_callback",
                "tts_provider_final",
                "tts_software_drain",
                "tts_estimated_audible",
            ),
        )
    finally:
        pipeline.close(wait_timeout_s=3.0)
        runtime.conn.close()
    start = "tts_request_started"
    provider_final = "tts_provider_final"
    accept = "tts_player_first_accept"
    llm_wait_ms = (llm_end_ns - llm_start_ns) / 1_000_000
    return RunMetrics(
        mode="system_smoke",
        request_to_session_open_ms=_relative_ms(points, start, "tts_session_opened"),
        request_to_provider_first_pcm_ms=_relative_ms(
            points,
            start,
            "tts_provider_first_pcm_received",
        ),
        request_to_player_accept_ms=_relative_ms(points, start, accept),
        request_to_first_callback_ms=_relative_ms(
            points,
            start,
            "audio_output_first_nonzero_callback",
        ),
        request_to_provider_final_ms=_relative_ms(points, start, provider_final),
        request_to_software_drain_ms=_relative_ms(points, start, "tts_software_drain"),
        request_to_estimated_audible_ms=_relative_ms(
            points,
            start,
            "tts_estimated_audible",
        ),
        player_accept_minus_provider_final_ms=_relative_ms(
            points,
            provider_final,
            accept,
        ),
        measurement_note=(
            f"batch LLM wait={llm_wait_ms:.3f}ms; TTS fields start only after the "
            "committed batch response. callback/audible semantics remain software-only"
        ),
    )


def main() -> int:  # noqa: C901 - explicit benchmark/fail-closed modes
    """Run real A/B against isolated state and emit machine-readable JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--text",
        default="这是 Jarvis 实时语音输出延迟测试。请保持同一段文本进行比较。",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--system-smoke", action="store_true")
    parser.add_argument("--bounded-smoke-only", action="store_true")
    parser.add_argument(
        "--system-utterance",
        default="请用一句简短中文回答：现在的语音输出测试目标是什么？",  # noqa: RUF001
    )
    parser.add_argument("--config", type=Path, default=Path("config/jarvis.yaml"))
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    config_path = args.config.resolve()
    media_config = _load_media_config(config_path)
    provenance, eligibility_reasons = _provenance(
        config_path=config_path,
        text=args.text,
        expected_revision=args.expected_revision,
        media_config=media_config,
    )
    if eligibility_reasons:
        ineligible_payload: dict[str, object] = {
            "schema": "jarvis.wave2.voice_latency.v2",
            "raw_runs": [],
            "summary": _summary([], eligibility_reasons=eligibility_reasons),
            "provenance": provenance,
            "state_isolation": "No provider/device run: provenance was ineligible",
        }
        encoded = json.dumps(
            ineligible_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if args.output is not None:
            args.output.write_text(encoded + "\n")
        print(encoded)  # noqa: T201 - operator-facing benchmark result
        return 2
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        parser.error("MINIMAX_API_KEY is required")
    if args.bounded_smoke_only:
        smoke = _bounded_real_streaming_smoke(api_key=api_key, text=args.text)
        smoke_payload: dict[str, object] = {
            "schema": "jarvis.wave2.voice_latency.v2",
            "raw_runs": [],
            "summary": _summary([], eligibility_reasons=[]),
            "bounded_real_streaming_smoke": smoke,
            "provenance": provenance,
            "state_isolation": "No Event Log opened; provider/device probe only",
        }
        encoded = json.dumps(
            smoke_payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        if args.output is not None:
            args.output.write_text(encoded + "\n")
        print(encoded)  # noqa: T201 - operator-facing benchmark result
        return 0 if smoke["status"] == "PASS" else 1
    with tempfile.TemporaryDirectory(prefix="jarvis-wave2-bench-") as tmp:
        workspace = Path(tmp)
        runs: list[RunMetrics] = []
        for index in range(args.runs):
            if index % 2 == 0:
                runs.append(_legacy_run(api_key=api_key, text=args.text))
                runs.append(
                    _streaming_run(
                        api_key=api_key,
                        text=args.text,
                        workspace=workspace / f"streaming-{index}",
                        media_config=media_config,
                    ),
                )
            else:
                runs.append(
                    _streaming_run(
                        api_key=api_key,
                        text=args.text,
                        workspace=workspace / f"streaming-{index}",
                        media_config=media_config,
                    ),
                )
                runs.append(_legacy_run(api_key=api_key, text=args.text))
        if args.system_smoke:
            runs.append(
                _system_smoke(
                    api_key=api_key,
                    utterance=args.system_utterance,
                    config_path=config_path,
                    workspace=workspace,
                    media_config=media_config,
                ),
            )
    payload: dict[str, object] = {
        "schema": "jarvis.wave2.voice_latency.v2",
        "raw_runs": [asdict(run) for run in runs],
        "summary": _summary(runs, eligibility_reasons=eligibility_reasons),
        "provenance": provenance,
        "state_isolation": "TemporaryDirectory deleted after run; no production DB",
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(encoded + "\n")
    print(encoded)  # noqa: T201 - operator-facing benchmark result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
