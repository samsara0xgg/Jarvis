"""Bounded live/default-mic and synthetic churn bench for ADR-0006 Wave 3.

This bench writes no Event Log, production JSONL, model cache, or audio file.
Its measurements stop at explicit software boundaries: PortAudio callback,
canonical subscriber delivery, and owner close.  RMS/min/max are captured PCM
diagnostics and are never reported as controlled physical acoustic truth.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import hashlib
import itertools
import json
import platform
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

import jarvis
from jarvis.surface import voice_audio, voice_backend

_RETAINED_HASH_COUNT = 8
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _json_default(value: object) -> object:
    """Encode typed enum outcomes without flattening their meaning."""
    if isinstance(value, enum.Enum):
        return value.value
    msg = f"unsupported bench JSON value: {type(value).__name__}"
    raise TypeError(msg)


def _git_provenance() -> dict[str, object]:
    """Read exact repository identity without trusting the caller's cwd."""
    head = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(  # noqa: S603 - fixed git argv, no shell
        ["git", "-C", str(_REPO_ROOT), "status", "--porcelain"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _eligibility(*, expected_revision: str) -> tuple[bool, dict[str, object]]:
    """Fail closed on revision, dirty tree, or cross-checkout imports."""
    git = _git_provenance()
    jarvis_path = Path(jarvis.__file__).resolve()
    voice_audio_path = Path(voice_audio.__file__).resolve()
    voice_backend_path = Path(voice_backend.__file__).resolve()
    identities = {
        "repo_root": str(_REPO_ROOT),
        "script_realpath": str(Path(__file__).resolve()),
        "script_sha256": _sha256_file(Path(__file__).resolve()),
        "jarvis_module_realpath": str(jarvis_path),
        "voice_audio_module_realpath": str(voice_audio_path),
        "voice_audio_module_sha256": _sha256_file(voice_audio_path),
        "voice_backend_module_realpath": str(voice_backend_path),
        "voice_backend_module_sha256": _sha256_file(voice_backend_path),
    }
    reasons: list[str] = []
    head = git.get("head")
    dirty = git.get("dirty")
    if not isinstance(head, str) or not head:
        reasons.append("git_head_unknown")
    elif head != expected_revision:
        reasons.append("revision_mismatch")
    if dirty is not False:
        reasons.append("worktree_dirty_or_unknown")
    for name, path in (
        ("jarvis", jarvis_path),
        ("voice_audio", voice_audio_path),
        ("voice_backend", voice_backend_path),
    ):
        if not path.is_relative_to(_REPO_ROOT):
            reasons.append(f"{name}_module_outside_repo_root")
    return not reasons, {
        "status": "ELIGIBLE" if not reasons else "INELIGIBLE",
        "expected_revision": expected_revision,
        "reasons": reasons,
        "git": git,
        "identity": identities,
    }


def _config_record(config: voice_audio.AudioIngressConfig) -> dict[str, object]:
    values = dataclasses.asdict(config)
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return {"effective": values, "sha256": hashlib.sha256(canonical.encode()).hexdigest()}


def _base_report(*, eligibility: dict[str, object]) -> dict[str, object]:
    """Return reproducibility metadata shared by live and synthetic runs."""
    import sounddevice  # noqa: PLC0415

    return {
        "record_kind": "wave3_audio_ingress_bench",
        "production_fact": False,
        "eligibility": eligibility,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sounddevice_version": getattr(sounddevice, "__version__", None),
        "measurement_limits": [
            "PortAudio callback arrival is not physical acoustic truth",
            "subscriber cursor is a software canonical timeline",
            "PCM RMS/min/max do not prove controlled speaker/microphone transfer",
            "typed close proves software/backend ownership only",
        ],
    }


def run_live_input_smoke(*, duration_s: float) -> dict[str, object]:
    """Open the default mic once and collect at least ``duration_s`` callbacks."""
    ingress_config = dataclasses.replace(
        voice_audio.AudioIngressConfig(),
        route_poll_s=max(1.0, duration_s + 0.5),
        shutdown_timeout_s=2.0,
    )
    input_format = voice_backend.AudioInputFormat(
        sample_rate_hz=ingress_config.canonical_sample_rate_hz,
        channels=1,
        callback_frame_samples=ingress_config.canonical_frame_samples,
    )
    backend = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=ingress_config.backend_open_timeout_s,
        close_timeout_s=ingress_config.backend_close_timeout_s,
    )
    ingress = voice_audio.AudioIngress(backend=backend, config=ingress_config)
    subscriber = ingress.subscribe(
        name="bounded-live-smoke",
        purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
        capacity=128,
    )
    started_at_ns = time.monotonic_ns()
    start = ingress.start()
    if not start.started:
        close = ingress.close()
        return {
            "status": "input_unavailable",
            "duration_target_s": duration_s,
            "config": _config_record(ingress_config),
            "backend_start": dataclasses.asdict(start.backend_result),
            "capability": dataclasses.asdict(start.capability),
            "close": dataclasses.asdict(close),
        }
    deadline = time.monotonic() + duration_s + 2.0
    target_samples = int(duration_s * ingress_config.canonical_sample_rate_hz)
    frames: list[voice_audio.CanonicalAudioFrame] = []
    observed_samples = 0
    while observed_samples < target_samples and time.monotonic() < deadline:
        frame = subscriber.read(timeout_s=0.05)
        if frame is None:
            continue
        frames.append(frame)
        observed_samples += frame.frame_count
    metrics_while_open = ingress.metrics()
    clock_mapping = ingress.clock_mapping()
    sleep_stop = ingress.stop_for_sleep()
    while True:
        frame = subscriber.read(timeout_s=0.01)
        if frame is None:
            break
        frames.append(frame)
        observed_samples += frame.frame_count
    metrics_before_close = ingress.metrics()
    subscriber.close()
    close = ingress.close()
    pcm = (
        np.concatenate(
            [np.frombuffer(frame.pcm16_mono, dtype="<i2") for frame in frames],
        )
        if frames
        else np.empty(0, dtype="<i2")
    )
    discontinuities = sum(frame.discontinuity_before for frame in frames)
    first_callback_delta_ms = (
        (metrics_while_open.first_callback_monotonic_ns - started_at_ns) / 1_000_000
        if metrics_while_open.first_callback_monotonic_ns is not None
        else None
    )
    profile = start.backend_result.profile
    profile_epoch = start.backend_result.stream_epoch
    cursors_contiguous = bool(frames) and frames[0].sample_cursor == 0 and all(
        current.sample_cursor == prior.sample_cursor + prior.frame_count
        for prior, current in itertools.pairwise(frames)
    )
    cursor_end = frames[-1].sample_cursor + frames[-1].frame_count if frames else None
    sum_frame_count = sum(frame.frame_count for frame in frames)
    ownership_after_close = backend.ownership_snapshot()
    strict_checks = {
        "target_samples_observed": observed_samples >= target_samples,
        "one_epoch_and_device": (
            profile is not None
            and profile_epoch == 1
            and all(frame.stream_epoch == profile_epoch for frame in frames)
        ),
        "cursor_starts_at_zero": bool(frames) and frames[0].sample_cursor == 0,
        "adjacent_cursor_continuity": cursors_contiguous,
        "cursor_end_equals_frame_sum": cursor_end == sum_frame_count,
        "native_overflows_zero": metrics_before_close.native_overflows == 0,
        "subscriber_overflows_zero": metrics_before_close.subscriber_overflows == 0,
        "callback_deadline_misses_zero": backend.callback_deadline_misses == 0,
        "callback_count_consistent": (
            backend.callback_count >= metrics_before_close.callback_calls
            and metrics_before_close.callback_frames
            == metrics_before_close.callback_calls * input_format.callback_frame_samples
        ),
        "canonical_subscriber_count_consistent": (
            metrics_before_close.callback_calls >= metrics_before_close.canonical_frames
            and metrics_before_close.canonical_frames == len(frames)
        ),
        "discontinuities_zero": discontinuities == 0,
        "sleep_stop_definitive": (
            sleep_stop is not None and sleep_stop.definitively_closed
        ),
        "backend_definitively_closed": (
            ownership_after_close.state is voice_backend.BackendLifecycleState.CLOSED
        ),
        "worker_definitively_closed": not close.worker_alive,
        "subscribers_definitively_closed": close.open_subscribers == 0,
        "ingress_definitively_closed": close.definitively_closed,
    }
    status = "observed" if all(strict_checks.values()) else "bounded_failure"
    return {
        "status": status,
        "duration_target_s": duration_s,
        "duration_observed_s": observed_samples / ingress_config.canonical_sample_rate_hz,
        "config": _config_record(ingress_config),
        "device": dataclasses.asdict(profile) if profile is not None else None,
        "stream_epoch": profile_epoch,
        "callback_calls": metrics_before_close.callback_calls,
        "callback_frames": metrics_before_close.callback_frames,
        "canonical_frames": metrics_before_close.canonical_frames,
        "subscriber_frames": len(frames),
        "cursor_start": frames[0].sample_cursor if frames else None,
        "cursor_end": cursor_end,
        "sum_frame_count": sum_frame_count,
        "first_callback_delta_ms": (
            round(first_callback_delta_ms, 3) if first_callback_delta_ms is not None else None
        ),
        "clock_mapping": (
            dataclasses.asdict(clock_mapping) if clock_mapping is not None else None
        ),
        "discontinuities": discontinuities,
        "native_overflows": metrics_before_close.native_overflows,
        "subscriber_overflows": metrics_before_close.subscriber_overflows,
        "late_epoch_callbacks_rejected": (metrics_before_close.late_epoch_callbacks_rejected),
        "callback_deadline_misses": backend.callback_deadline_misses,
        "backend_callback_count": backend.callback_count,
        "pcm_min": int(np.min(pcm)) if pcm.size else None,
        "pcm_max": int(np.max(pcm)) if pcm.size else None,
        "pcm_rms": (
            round(float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2))), 3) if pcm.size else None
        ),
        "close": dataclasses.asdict(close),
        "sleep_stop": dataclasses.asdict(sleep_stop) if sleep_stop is not None else None,
        "strict_checks": strict_checks,
    }


class _SyntheticCallbackBackend:
    """In-process callback source for real thread/ring/epoch churn."""

    def __init__(self) -> None:
        self.format = voice_backend.AudioInputFormat(16_000, 1, 512)
        self.sinks: dict[int, voice_backend.InputFrameSink] = {}
        self.attempts: dict[int, str] = {}
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.ownership_version = 0
        self.start_count = 0
        self.stop_count = 0
        self.active_owners = 0
        self.max_active_owners = 0

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: voice_backend.InputFrameSink,
        render_source: voice_backend.RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStartResult:
        del timeout_s
        if render_source is not None or self.active_epoch is not None:
            return voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OWNER_BUSY,
                stream_epoch=stream_epoch,
                profile=None,
                reason="synthetic_owner_busy",
            )
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.ownership_version += 1
        self.sinks[stream_epoch] = frame_sink
        self.attempts[stream_epoch] = attempt_id
        self.start_count += 1
        self.active_owners += 1
        self.max_active_owners = max(self.max_active_owners, self.active_owners)
        profile = voice_backend.InputDeviceProfile(
            device_uid="synthetic-callback",
            device_name="synthetic-callback",
            backend="synthetic",
            input_format=self.format,
        )
        return voice_backend.BackendStartResult(
            status=voice_backend.BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=profile,
            attempt_id=attempt_id,
        )

    def stop(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStopResult:
        del timeout_s
        self.stop_count += 1
        if self.active_epoch == stream_epoch and self.active_attempt_id == attempt_id:
            self.active_epoch = None
            self.active_attempt_id = None
            self.active_owners -= 1
            self.ownership_version += 1
        return voice_backend.BackendStopResult(
            status=voice_backend.BackendStopStatus.CLOSED,
            stream_epoch=stream_epoch,
            attempt_id=attempt_id,
        )

    def emit(self, *, stream_epoch: int, value: int) -> None:
        callback_owned = bytearray(np.full(512, value, dtype="<i2").tobytes())
        self.sinks[stream_epoch](
            stream_epoch=stream_epoch,
            attempt_id=self.attempts[stream_epoch],
            callback_buffer=callback_owned,
            frame_count=512,
            adc_time_s=None,
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=False,
        )
        callback_owned[:] = b"\x5a" * len(callback_owned)

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        del stream_epoch
        return None

    def current_device_uid(self) -> str | None:
        return "synthetic-callback"

    def input_format(self) -> voice_backend.AudioInputFormat:
        return self.format

    def output_format(self) -> None:
        return None

    def capabilities(self) -> voice_backend.BackendCapabilities:
        return voice_backend.BackendCapabilities(
            owns_default_input=True,
            owns_render_clock=False,
            aec=False,
            natural_barge_in=False,
            reliable_adc_time=False,
            reliable_dac_time=False,
        )

    def ownership_snapshot(self) -> voice_backend.BackendOwnershipSnapshot:
        return voice_backend.BackendOwnershipSnapshot(
            state=(
                voice_backend.BackendLifecycleState.CLOSED
                if self.active_epoch is None
                else voice_backend.BackendLifecycleState.OPEN
            ),
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.ownership_version,
            physical_owner_possible=self.active_epoch is not None,
            helper_thread_alive=False,
        )


def _read_one(
    subscriber: voice_audio.AudioSubscription,
    *,
    deadline_s: float,
) -> voice_audio.CanonicalAudioFrame | None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        frame = subscriber.read(timeout_s=0.01)
        if frame is not None:
            return frame
    return None


def run_synthetic_churn(  # noqa: C901, PLR0912, PLR0915 - one auditable bounded bench scenario
    *,
    cycles: int,
) -> dict[str, object]:
    """Exercise real worker/ring/subscription concurrency for ``cycles`` epochs."""
    backend = _SyntheticCallbackBackend()
    config = dataclasses.replace(
        voice_audio.AudioIngressConfig(),
        native_ring_capacity=8,
        default_subscriber_capacity=2,
        worker_poll_s=0.0005,
        route_poll_s=60.0,
        shutdown_timeout_s=1.0,
    )
    ingress = voice_audio.AudioIngress(backend=backend, config=config)
    started = ingress.start()
    errors: list[str] = []
    retained_hashes: list[str] = []
    cycles_completed = 0
    callback_requests: queue.Queue[tuple[int, int, threading.Event] | None] = queue.Queue(
        maxsize=1,
    )

    def _callback_loop() -> None:
        while True:
            request = callback_requests.get()
            try:
                if request is None:
                    return
                stream_epoch, value, finished = request
                backend.emit(stream_epoch=stream_epoch, value=value)
                finished.set()
            finally:
                callback_requests.task_done()

    callback_thread = threading.Thread(
        target=_callback_loop,
        name="jarvis-wave3-bench-callback",
        daemon=False,
    )
    callback_thread.start()

    def _emit_on_callback_thread(*, stream_epoch: int, value: int) -> bool:
        finished = threading.Event()
        try:
            callback_requests.put((stream_epoch, value, finished), timeout=0.5)
        except queue.Full:
            return False
        return finished.wait(timeout=0.5)

    started_at = time.monotonic()
    if started.started:
        for cycle in range(cycles):
            subscriber = ingress.subscribe(
                name=f"churn-{cycle}",
                purpose=voice_audio.SubscriberPurpose.CAPTURE,
                capacity=2,
            )
            epoch = ingress.stream_epoch
            if epoch is None:
                errors.append(f"cycle_{cycle}:missing_epoch")
                subscriber.close()
                break
            value = cycle % 30_000
            if not _emit_on_callback_thread(stream_epoch=epoch, value=value):
                errors.append(f"cycle_{cycle}:callback_thread_timeout")
                subscriber.close()
                break
            frame = _read_one(subscriber, deadline_s=0.5)
            if frame is None:
                errors.append(f"cycle_{cycle}:subscriber_timeout")
            else:
                if frame.stream_epoch != epoch or frame.sample_cursor != 0:
                    errors.append(f"cycle_{cycle}:cursor_or_epoch_pollution")
                if cycle < _RETAINED_HASH_COUNT:
                    retained_hashes.append(hashlib.sha256(frame.pcm16_mono).hexdigest())
            subscriber.close()
            stop = ingress.stop_for_sleep()
            reopen = ingress.resume_after_wake()
            if stop is None or not stop.definitively_closed:
                errors.append(f"cycle_{cycle}:stop_not_closed")
                break
            if reopen is None or not reopen.started:
                errors.append(f"cycle_{cycle}:reopen_failed")
                break
            if not _emit_on_callback_thread(stream_epoch=epoch, value=31_000):
                errors.append(f"cycle_{cycle}:late_callback_thread_timeout")
                break
            cycles_completed += 1
    metrics = ingress.metrics()
    close = ingress.close()
    try:
        callback_requests.put(None, timeout=0.5)
    except queue.Full:
        errors.append("callback_shutdown_queue_full")
    callback_thread.join(timeout=1.0)
    if callback_thread.is_alive():
        errors.append("callback_thread_leaked")
    expected_hashes = [
        hashlib.sha256(np.full(512, value, dtype="<i2").tobytes()).hexdigest()
        for value in range(min(_RETAINED_HASH_COUNT, cycles))
    ]
    if retained_hashes != expected_hashes:
        errors.append("retained_buffer_mutation")
    strict_checks = {
        "cycles_completed": cycles_completed == cycles,
        "late_epoch_callbacks_rejected": (
            metrics.late_epoch_callbacks_rejected == cycles
        ),
        "start_count": backend.start_count == cycles + 1,
        "stop_count": backend.stop_count == cycles + 1,
        "max_active_input_owners": backend.max_active_owners == 1,
        "native_overflows": metrics.native_overflows == 0,
        "subscriber_overflows": metrics.subscriber_overflows == 0,
        "callback_thread_closed": not callback_thread.is_alive(),
        "backend_owner_closed": backend.active_owners == 0,
        "ingress_definitively_closed": close.definitively_closed,
        "subscribers_closed": close.open_subscribers == 0,
    }
    failed_checks = [name for name, passed in strict_checks.items() if not passed]
    errors.extend(f"strict_gate:{name}" for name in failed_checks)
    return {
        "status": (
            "observed"
            if started.started
            and not errors
            and all(strict_checks.values())
            else "bounded_failure"
        ),
        "cycles_requested": cycles,
        "cycles_completed": cycles_completed,
        "wall_s": round(time.monotonic() - started_at, 3),
        "config": _config_record(config),
        "start_count": backend.start_count,
        "stop_count": backend.stop_count,
        "max_active_input_owners": backend.max_active_owners,
        "late_epoch_callbacks_rejected": metrics.late_epoch_callbacks_rejected,
        "callback_thread_alive_after_close": callback_thread.is_alive(),
        "native_overflows": metrics.native_overflows,
        "subscriber_overflows": metrics.subscriber_overflows,
        "retained_buffer_hashes": retained_hashes,
        "errors": errors,
        "strict_checks": strict_checks,
        "close": dataclasses.asdict(close),
    }


def main(argv: list[str] | None = None) -> int:
    """Run selected bounded probes and emit one JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-input", action="store_true")
    parser.add_argument("--duration-s", type=float, default=1.0)
    parser.add_argument("--synthetic-churn-cycles", type=int, default=0)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live_input and args.synthetic_churn_cycles <= 0:
        parser.error("select --live-input and/or --synthetic-churn-cycles")
    if args.duration_s <= 0 or args.synthetic_churn_cycles < 0:
        parser.error("duration must be positive and churn cycles non-negative")
    eligible, eligibility = _eligibility(expected_revision=args.expected_revision)
    if not eligible:
        report = {
            "record_kind": "wave3_audio_ingress_bench",
            "production_fact": False,
            "status": "INELIGIBLE",
            "eligibility": eligibility,
        }
        payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.write_text(payload, encoding="utf-8")
        sys.stdout.write(payload)
        return 2
    report = _base_report(eligibility=eligibility)
    if args.live_input:
        report["live_input"] = run_live_input_smoke(duration_s=args.duration_s)
    if args.synthetic_churn_cycles:
        report["synthetic_churn"] = run_synthetic_churn(
            cycles=args.synthetic_churn_cycles,
        )
    payload = (
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        )
        + "\n"
    )
    if args.output is not None:
        args.output.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    results = [
        value.get("status")
        for key in ("live_input", "synthetic_churn")
        if isinstance((value := report.get(key)), dict)
    ]
    return 0 if results and all(status == "observed" for status in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
