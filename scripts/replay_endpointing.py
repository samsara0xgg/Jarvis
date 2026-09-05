"""Replay WAV fixtures through the real endpointing path (ADR-0006 D7 live check).

Seed of the ADR-0006 §10.3 Tier 2 corpus runner. Each WAV is fed by
``FileReplayBackend`` at real-time pace through the real ``AudioIngress``,
Silero VAD, ``UtteranceAssembler`` partial-ASR hold, and SenseVoice final
ASR into a throwaway Event Log. The wake engine is an always-on stub, not
the real wake model: this script measures endpointing, not wake detection.
Every printed row is diagnostic; the ``utterance.received`` rows come from
the temporary Event Log created for the run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
import time
import wave
from collections.abc import Mapping
from pathlib import Path

import yaml

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_backend, voice_pipeline, voice_session

_TRAIL_NAMES = frozenset(
    {
        "audio_input_capture_started",
        "endpoint_decision",
        "asr_partial",
        "partial_asr_degraded",
        "endpoint_candidate",
        "audio_input_endpoint_committed",
        "asr_final",
        "utterance_committed",
        "audio_input_wake_arm_expired",
        "audio_input_discontinuity",
    },
)
_HIDDEN_ATTRIBUTES = frozenset({"session_id", "measurement_boundary"})


class _AlwaysOnWake:
    """Arm the assembler on every wake window; deliberately not the real model."""

    model_name = "replay"

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        return {self.model_name: 1.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def _ingress_values(config_path: Path, *, partial_asr: bool) -> dict[str, object]:
    with config_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    realtime = raw.get("realtime") if isinstance(raw, Mapping) else None
    ingress = realtime.get("single_audio_ingress") if isinstance(realtime, Mapping) else None
    values: dict[str, object] = dict(ingress) if isinstance(ingress, Mapping) else {}
    partial_raw = values.get("partial_asr")
    partial: dict[str, object] = dict(partial_raw) if isinstance(partial_raw, Mapping) else {}
    partial["enabled"] = partial_asr
    values["partial_asr"] = partial
    return values


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / reader.getframerate()


def _print_trail() -> None:
    points = [point for point in realtime_trace_snapshot() if point.name in _TRAIL_NAMES]
    if not points:
        sys.stdout.write("  (no trail points)\n")
        return
    origin = points[0].monotonic_ns
    for point in points:
        attributes = " ".join(
            f"{key}={value}"
            for key, value in sorted(point.attributes.items())
            if key not in _HIDDEN_ATTRIBUTES
        )
        offset_ms = (point.monotonic_ns - origin) / 1_000_000
        sys.stdout.write(f"  +{offset_ms:8.1f}ms {point.name} {attributes}\n")


def _print_utterances(db_path: Path, *, since_id: int) -> list[str]:
    reasons: list[str] = []
    with contextlib.closing(open_event_log(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM events WHERE type = 'utterance.received' AND id > ?"
            " ORDER BY id",
            (since_id,),
        ).fetchall()
    for _, payload_json in rows:
        payload = json.loads(payload_json)
        reasons.append(str(payload.get("endpoint_reason")))
        sys.stdout.write(
            "  utterance.received utterance_id={} endpoint_reason={} transcript={!r}\n".format(
                payload.get("utterance_id"),
                payload.get("endpoint_reason"),
                payload.get("transcript"),
            ),
        )
    return reasons


def _last_event_id(db_path: Path) -> int:
    with contextlib.closing(open_event_log(db_path)) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    return int(row[0]) if row else 0


def _replay_one(  # noqa: PLR0913 - one composition root per WAV
    wav: Path,
    *,
    values: Mapping[str, object],
    root: Path,
    sensevoice_dir: Path,
    silero: Path,
    tail_s: float,
) -> None:
    reset_realtime_trace()
    db_path = root / "events.db"
    since_id = _last_event_id(db_path)
    backend = voice_backend.FileReplayBackend(wav, tail_silence_s=None)
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=voice_audio.audio_ingress_config_from_mapping(values),
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=voice_asr.SenseVoiceRecognizer(model_dir=sensevoice_dir),
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=root / "voice_artifacts",
    )
    pipeline.prewarm_input_model()
    session = voice_session.DuplexVoiceSession(
        ingress=ingress,
        wake_engine=_AlwaysOnWake(),
        vad=voice_audio.SileroVad(mode="record", model_path=silero),
        pipeline=pipeline,
        broadcaster=None,
        output_active=lambda: False,
        wake_threshold=0.5,
        config=voice_session.realtime_input_session_config_from_mapping(values),
    )
    sys.stdout.write(f"== {wav.name} ({_wav_duration_s(wav):.2f}s + {tail_s:.1f}s tail)\n")
    started = session.start()
    if not started.started:
        sys.stdout.write(f"  session start failed: {started.ingress.capability.reason}\n")
        return
    time.sleep(_wav_duration_s(wav) + tail_s)
    close = session.close()
    _print_trail()
    reasons = _print_utterances(db_path, since_id=since_id)
    metrics = session.metrics()
    sys.stdout.write(
        f"{wav.name}: utterances={len(reasons)} reasons={reasons} "
        f"partial_decodes={metrics.partial_decodes} drops={metrics.partial_snapshot_drops} "
        f"closed={close.definitively_closed}\n",
    )


def main(argv: list[str] | None = None) -> int:
    """Replay each WAV in order and print its endpointing trail."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", type=Path, action="append", required=True)
    parser.add_argument("--config", type=Path, default=Path("config/jarvis.yaml"))
    parser.add_argument("--sensevoice-dir", type=Path, default=Path("data/sensevoice-small-int8"))
    parser.add_argument("--silero", type=Path, default=Path("data/silero_vad.onnx"))
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--tail-s", type=float, default=3.0)
    parser.add_argument(
        "--partial-asr",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args(argv)
    root = args.runtime_root or Path(tempfile.mkdtemp(prefix="jarvis-replay-"))
    root.mkdir(parents=True, exist_ok=True)
    values = _ingress_values(args.config, partial_asr=args.partial_asr)
    sys.stdout.write(f"runtime_root={root} partial_asr={args.partial_asr}\n")
    for wav in args.wav:
        _replay_one(
            wav,
            values=values,
            root=root,
            sensevoice_dir=args.sensevoice_dir,
            silero=args.silero,
            tail_s=args.tail_s,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
