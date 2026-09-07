"""ADR-0006 Wave 3 integration acceptance for one continuous audio ingress."""

# ruff: noqa: SLF001 - private owner/ring seams are the race surface under test.
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_interrupt,
    voice_media,
    voice_pipeline,
    voice_session,
    voice_tts,
    voice_wake,
)
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps
from scripts import bench_voice_audio_ingress as voice_input_bench
from tools.realtime_trace_report import TraceRow, summarize_trace

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.001)


def _pcm(value: int, *, frames: int = voice_audio.SILERO_CHUNK_SAMPLES) -> bytes:
    return np.full(frames, value, dtype="<i2").tobytes()


class _FakeBackend:
    """Callback-capable backend with exact owner/fault/epoch accounting."""

    def __init__(
        self,
        *,
        stop_status: voice_backend.BackendStopStatus = voice_backend.BackendStopStatus.CLOSED,
    ) -> None:
        self.format = voice_backend.AudioInputFormat(16_000, 1, 512)
        self.stop_status = stop_status
        self.sinks: dict[int, voice_backend.InputFrameSink] = {}
        self.attempts: dict[int, str] = {}
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.device_uid = "fake-input-A"
        self.pending_fault: voice_backend.BackendFault | None = None
        self.start_count = 0
        self.stop_count = 0
        self.active_owner_count = 0
        self.max_active_owner_count = 0
        self.ownership_version = 0

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
        assert render_source is None
        assert self.active_epoch is None, "a second physical owner was opened"
        self.start_count += 1
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.ownership_version += 1
        self.active_owner_count += 1
        self.max_active_owner_count = max(
            self.max_active_owner_count,
            self.active_owner_count,
        )
        self.sinks[stream_epoch] = frame_sink
        self.attempts[stream_epoch] = attempt_id
        profile = voice_backend.InputDeviceProfile(
            device_uid=self.device_uid,
            device_name=self.device_uid,
            backend="fake",
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
        if (
            self.active_epoch == stream_epoch
            and self.active_attempt_id == attempt_id
            and self.stop_status is voice_backend.BackendStopStatus.CLOSED
        ):
            self.active_epoch = None
            self.active_attempt_id = None
            self.active_owner_count -= 1
            self.ownership_version += 1
        return voice_backend.BackendStopResult(
            status=self.stop_status,
            stream_epoch=stream_epoch,
            reason=(
                "injected_stuck_backend"
                if self.stop_status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
                else None
            ),
            attempt_id=attempt_id,
        )

    def emit(
        self,
        *,
        epoch: int,
        value: int,
        discontinuity: bool = False,
    ) -> None:
        callback_owned = bytearray(_pcm(value))
        self.sinks[epoch](
            stream_epoch=epoch,
            attempt_id=self.attempts[epoch],
            callback_buffer=callback_owned,
            frame_count=512,
            adc_time_s=time.monotonic(),
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=discontinuity,
        )
        # If ingress retained a borrowed callback buffer, every subscriber
        # would now observe this mutation instead of ``value``.
        callback_owned[:] = b"\x7f" * len(callback_owned)

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        fault = self.pending_fault
        if fault is not None and fault.stream_epoch == stream_epoch:
            self.pending_fault = None
            return fault
        return None

    def current_device_uid(self) -> str | None:
        return self.device_uid

    def current_output_route(self) -> voice_backend.OutputRoute | None:
        return None

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
            reliable_adc_time=True,
            reliable_dac_time=False,
        )

    def ownership_snapshot(self) -> voice_backend.BackendOwnershipSnapshot:
        state = (
            voice_backend.BackendLifecycleState.CLOSED
            if self.active_epoch is None
            else voice_backend.BackendLifecycleState.OPEN
            if self.stop_status is voice_backend.BackendStopStatus.CLOSED
            else voice_backend.BackendLifecycleState.UNCERTAIN
        )
        return voice_backend.BackendOwnershipSnapshot(
            state=state,
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.ownership_version,
            physical_owner_possible=self.active_epoch is not None,
            helper_thread_alive=False,
            reason=(
                "injected_stuck_backend"
                if self.active_epoch is not None
                and state is voice_backend.BackendLifecycleState.UNCERTAIN
                else None
            ),
        )


class _ScriptedWakeBackend(_FakeBackend):
    """Inject one typed wake-open result while retaining real owner accounting."""

    def __init__(self) -> None:
        super().__init__()
        self.next_start_status: voice_backend.BackendStartStatus | None = None

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: voice_backend.InputFrameSink,
        render_source: voice_backend.RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStartResult:
        status = self.next_start_status
        if status is None:
            return super().start(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                frame_sink=frame_sink,
                render_source=render_source,
                timeout_s=timeout_s,
            )
        self.next_start_status = None
        if status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN:
            super().start(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                frame_sink=frame_sink,
                render_source=render_source,
                timeout_s=timeout_s,
            )
        else:
            self.start_count += 1
        return voice_backend.BackendStartResult(
            status=status,
            stream_epoch=stream_epoch,
            profile=None,
            reason=f"injected_{status.value}",
            attempt_id=attempt_id,
        )


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def __init__(self) -> None:
        self.input_h_values: list[float] = []
        self.input_c_values: list[float] = []

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.input_h_values.append(float(inputs["h"].flat[0]))
        self.input_c_values.append(float(inputs["c"].flat[0]))
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy() + 1.0,
            inputs["c"].copy() + 10.0,
        ]


class _FakeWakeEngine:
    """Detect selected wake windows and retain reset/close proof."""

    model_name = "hey_jarvis_v0.1"

    def __init__(self, detections: set[int] | None = None) -> None:
        self._detections = detections
        self.calls = 0
        self.resets = 0
        self.closed = False

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        call = self.calls
        self.calls += 1
        detected = self._detections is None or call in self._detections
        return {self.model_name: 0.9 if detected else 0.0}

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closed = True


class _RecordingPipeline:
    """Whole-WAV adapter fixture; never owns a hardware stream."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(dict(kwargs))
        return MagicMock()


def _ingress(
    backend: _FakeBackend,
    *,
    native_capacity: int = 64,
    subscriber_capacity: int = 16,
) -> voice_audio.AudioIngress:
    return voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            native_ring_capacity=native_capacity,
            default_subscriber_capacity=subscriber_capacity,
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.002,
            shutdown_timeout_s=1.0,
        ),
    )


def _read_frames(
    subscription: voice_audio.AudioSubscription,
    count: int,
) -> list[voice_audio.CanonicalAudioFrame]:
    frames: list[voice_audio.CanonicalAudioFrame] = []
    deadline = time.monotonic() + 2.0
    while len(frames) < count and time.monotonic() < deadline:
        frame = subscription.read(timeout_s=0.02)
        if frame is not None:
            frames.append(frame)
    assert len(frames) == count
    return frames


def test_sounddevice_backend_asserts_one_default_input_owner_and_uses_callback() -> None:
    """Two production backends cannot concurrently own the default mic."""

    class _Stream:
        def __init__(self, callback: Callable[..., None]) -> None:
            self.callback = callback
            self.active = False
            self.closed = False

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.closed = True

    streams: list[_Stream] = []

    def _open(
        *,
        input_format: voice_backend.AudioInputFormat,
        device_index: int | None,
        callback: Callable[..., None],
        finished_callback: Callable[[], None],
    ) -> _Stream:
        del input_format, finished_callback
        assert device_index is None
        stream = _Stream(callback)
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "device-A",
        "device-A",
        "sounddevice",
        input_format,
    )
    first = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    second = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    copied: list[bytes] = []

    def _sink(  # noqa: PLR0913 - mirrors the fixed backend sink contract
        *,
        stream_epoch: int,
        attempt_id: str,
        callback_buffer: Any,  # noqa: ANN401 - mirrors untyped sounddevice buffer
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
    ) -> None:
        del (
            stream_epoch,
            attempt_id,
            frame_count,
            adc_time_s,
            captured_monotonic_ns,
            discontinuity_before,
        )
        copied.append(bytes(memoryview(callback_buffer)))

    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        assert first.start(stream_epoch=1, attempt_id="first-1", frame_sink=_sink).started
        busy = second.start(stream_epoch=1, attempt_id="second-1", frame_sink=_sink)
        assert busy.status is voice_backend.BackendStartStatus.OWNER_BUSY
        streams[0].callback(
            bytearray(_pcm(123)),
            512,
            MagicMock(inputBufferAdcTime=1.25),
            MagicMock(input_overflow=False, __bool__=lambda _self: False),
        )
        assert copied == [_pcm(123)]
        assert first.stop(stream_epoch=1, attempt_id="first-1").definitively_closed
        assert second.start(stream_epoch=2, attempt_id="second-2", frame_sink=_sink).started
        assert second.stop(stream_epoch=2, attempt_id="second-2").definitively_closed
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


def test_sounddevice_stale_stop_cannot_retarget_successor_epoch() -> None:
    """A late stop for N/A is a typed no-op after N+1/B becomes OPEN."""

    class _Stream:
        active = False

        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.close_calls += 1

    streams = [_Stream(), _Stream()]
    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "exact-stop-device",
        "exact-stop-device",
        "sounddevice",
        input_format,
    )
    with (
        patch.object(
            voice_backend,
            "_open_sounddevice_input_stream",
            side_effect=streams,
        ),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=input_format,
            open_timeout_s=0.5,
            close_timeout_s=0.5,
        )
        assert backend.start(stream_epoch=1, attempt_id="A", frame_sink=MagicMock()).started
        assert backend.stop(stream_epoch=1, attempt_id="A").definitively_closed
        assert backend.start(stream_epoch=2, attempt_id="B", frame_sink=MagicMock()).started
        stale = backend.stop(stream_epoch=1, attempt_id="A")
        assert stale.status is voice_backend.BackendStopStatus.STALE_ATTEMPT
        assert not stale.definitively_closed
        assert streams[1].close_calls == 0
        snapshot = backend.ownership_snapshot()
        assert snapshot.stream_epoch == 2
        assert snapshot.attempt_id == "B"
        assert backend.stop(stream_epoch=2, attempt_id="B").definitively_closed


def test_sounddevice_close_timeout_joins_one_helper_then_serializes_retry() -> None:
    """UNCERTAIN replays an in-flight close and permits one later failed retry."""
    close_entered = threading.Event()
    close_release = threading.Event()

    class _Stream:
        active = False

        def __init__(self) -> None:
            self.abort_calls = 0
            self.close_calls = 0
            self.fail_next_close = False

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.abort_calls += 1
            self.active = False

        def close(self) -> None:
            self.close_calls += 1
            close_entered.set()
            assert close_release.wait(timeout=1.0)
            if self.fail_next_close:
                self.fail_next_close = False
                message = "injected close failure"
                raise OSError(message)

    stream = _Stream()
    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "join-close-device",
        "join-close-device",
        "sounddevice",
        input_format,
    )
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", return_value=stream),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=input_format,
            open_timeout_s=0.5,
            close_timeout_s=0.01,
        )
        assert backend.start(stream_epoch=1, attempt_id="A", frame_sink=MagicMock()).started
        first = backend.stop(stream_epoch=1, attempt_id="A")
        assert not first.definitively_closed
        assert close_entered.is_set()
        joined: list[voice_backend.BackendStopResult] = []
        closers = [
            threading.Thread(
                target=lambda: joined.append(
                    backend.stop(stream_epoch=1, attempt_id="A"),
                ),
            )
            for _ in range(2)
        ]
        for closer in closers:
            closer.start()
        for closer in closers:
            closer.join(timeout=1.0)
        assert len(joined) == 2
        assert all(not result.definitively_closed for result in joined)
        assert stream.abort_calls == 1
        assert stream.close_calls == 1
        close_release.set()
        _wait_until(
            lambda: backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED,
        )

    retry_stream = _Stream()
    retry_stream.fail_next_close = True
    close_release.set()
    with (
        patch.object(
            voice_backend,
            "_open_sounddevice_input_stream",
            return_value=retry_stream,
        ),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=input_format,
            open_timeout_s=0.5,
            close_timeout_s=0.5,
        )
        assert backend.start(stream_epoch=3, attempt_id="C", frame_sink=MagicMock()).started
        failed = backend.stop(stream_epoch=3, attempt_id="C")
        assert not failed.definitively_closed
        assert retry_stream.close_calls == 1
        retried = backend.stop(stream_epoch=3, attempt_id="C")
        assert retried.definitively_closed
        assert retry_stream.close_calls == 2


def test_sounddevice_abort_error_still_attempts_and_proves_close() -> None:
    """Foreign abort failure is advisory when exact close returns."""

    class _Stream:
        active = False

        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            message = "injected abort failure"
            raise OSError(message)

        def close(self) -> None:
            self.close_calls += 1

    stream = _Stream()
    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile("abort-device", "abort-device", "sd", input_format)
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", return_value=stream),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        backend = voice_backend.SoundDeviceDuplexBackend(
            input_format=input_format,
            open_timeout_s=0.5,
            close_timeout_s=0.5,
        )
        assert backend.start(stream_epoch=1, attempt_id="A", frame_sink=MagicMock()).started
        assert backend.stop(stream_epoch=1, attempt_id="A").definitively_closed
        assert stream.close_calls == 1


def test_sounddevice_route_change_during_open_closes_stream_before_owner_release() -> None:
    """An open-time default-device race cannot mix old hardware/new profile."""

    class _Stream:
        active = False
        closed = False

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.closed = True

    streams: list[_Stream] = []

    def _open(**_kwargs: object) -> _Stream:
        stream = _Stream()
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    backend = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    successor = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    first_profile = voice_backend.InputDeviceProfile(
        "device-A",
        "device-A",
        "sounddevice",
        input_format,
    )
    second_profile = voice_backend.InputDeviceProfile(
        "device-B",
        "device-B",
        "sounddevice",
        input_format,
    )
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(
            voice_backend,
            "_default_input_device_profile",
            side_effect=[first_profile, second_profile, second_profile, second_profile],
        ),
    ):
        failed = backend.start(stream_epoch=1, attempt_id="route-1", frame_sink=MagicMock())
        assert failed.status is voice_backend.BackendStartStatus.FAILED_CLOSED
        assert streams[0].closed
        assert successor.start(stream_epoch=2, attempt_id="route-2", frame_sink=MagicMock()).started
        assert successor.stop(stream_epoch=2, attempt_id="route-2").definitively_closed


def test_sounddevice_open_timeout_late_success_closes_before_owner_release() -> None:
    """A late foreign open is closed and cannot overlap a successor owner."""

    class _Stream:
        def __init__(self) -> None:
            self.active = False
            self.closed = False

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.closed = True

    first_open_entered = threading.Event()
    release_first_open = threading.Event()
    streams: list[_Stream] = []

    def _open(**_kwargs: object) -> _Stream:
        if not streams:
            first_open_entered.set()
            assert release_first_open.wait(timeout=0.5)
        stream = _Stream()
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "device-timeout",
        "device-timeout",
        "sounddevice",
        input_format,
    )
    timed_out_backend = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.01,
        close_timeout_s=0.5,
    )
    successor = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        timed_out = timed_out_backend.start(
            stream_epoch=1,
            attempt_id="timeout-1",
            frame_sink=MagicMock(),
        )
        assert first_open_entered.is_set()
        assert timed_out.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
        assert (
            successor.start(
                stream_epoch=2,
                attempt_id="timeout-2",
                frame_sink=MagicMock(),
            ).status
            is voice_backend.BackendStartStatus.OWNER_BUSY
        )
        release_first_open.set()
        _wait_until(lambda: bool(streams) and streams[0].closed)
        assert successor.start(
            stream_epoch=3,
            attempt_id="timeout-3",
            frame_sink=MagicMock(),
        ).started
        assert successor.stop(
            stream_epoch=3,
            attempt_id="timeout-3",
        ).definitively_closed
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


def test_sounddevice_close_timeout_releases_owner_only_after_late_close() -> None:
    """A close timeout stays uncertain but never overlaps the next stream."""

    class _Stream:
        def __init__(self, *, block_close: bool) -> None:
            self.active = False
            self.closed = False
            self._block_close = block_close

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            if self._block_close:
                close_entered.set()
                assert release_close.wait(timeout=0.5)
            self.closed = True

    close_entered = threading.Event()
    release_close = threading.Event()
    streams: list[_Stream] = []

    def _open(**_kwargs: object) -> _Stream:
        stream = _Stream(block_close=not streams)
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "device-close-timeout",
        "device-close-timeout",
        "sounddevice",
        input_format,
    )
    first = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.01,
    )
    successor = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    )
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        assert first.start(stream_epoch=1, attempt_id="close-1", frame_sink=MagicMock()).started
        close = first.stop(stream_epoch=1, attempt_id="close-1")
        assert close_entered.is_set()
        assert close.status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
        assert (
            successor.start(
                stream_epoch=2,
                attempt_id="close-2",
                frame_sink=MagicMock(),
            ).status
            is voice_backend.BackendStartStatus.OWNER_BUSY
        )
        release_close.set()
        _wait_until(lambda: streams[0].closed)
        assert successor.start(
            stream_epoch=3,
            attempt_id="close-3",
            frame_sink=MagicMock(),
        ).started
        assert successor.stop(
            stream_epoch=3,
            attempt_id="close-3",
        ).definitively_closed
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


def test_wake_capture_diagnostics_share_cursor_and_own_callback_buffers() -> None:
    """All subscriber lanes observe one cursor and callback mutation is isolated."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    wake = ingress.subscribe(name="wake-test", purpose=voice_audio.SubscriberPurpose.WAKE)
    capture = ingress.subscribe(
        name="capture-test",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
    )
    diagnostics = ingress.subscribe(
        name="diagnostics-test",
        purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    for value in range(1, 7):
        backend.emit(epoch=epoch, value=value)
    wake_frames = _read_frames(wake, 6)
    capture_frames = _read_frames(capture, 6)
    diagnostic_frames = _read_frames(diagnostics, 6)
    cursors = [frame.sample_cursor for frame in wake_frames]
    assert cursors == [0, 512, 1024, 1536, 2048, 2560]
    assert [frame.sample_cursor for frame in capture_frames] == cursors
    assert [frame.sample_cursor for frame in diagnostic_frames] == cursors
    mapping = ingress.clock_mapping()
    assert mapping is not None
    assert mapping.stream_epoch == epoch
    assert mapping.input_sample_cursor == 0
    assert mapping.adc_time_s is not None
    assert mapping.monotonic_ns > 0
    assert [np.frombuffer(frame.pcm16_mono, dtype="<i2")[0] for frame in wake_frames] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    retained = wake_frames[0].pcm16_mono
    for value in range(7, 30):
        backend.emit(epoch=epoch, value=value)
    assert retained == _pcm(1), "consumer retained a wrapped/callback-owned buffer"
    result = ingress.close()
    assert result.definitively_closed
    assert result.open_subscribers == 0
    assert backend.max_active_owner_count == 1


def test_wake_window_and_pre_roll_are_contiguous_without_missing_or_duplicate() -> None:
    """32 ms input becomes continuous 80 ms wake windows and gap-free utterance PCM."""
    framer = voice_session.WakeWindowFramer()
    source = b"".join(_pcm(value) for value in range(1, 11))
    windows: list[bytes] = []
    end_cursors: list[int] = []
    for sequence in range(10):
        frame = voice_audio.CanonicalAudioFrame(
            stream_epoch=1,
            sequence=sequence,
            sample_cursor=sequence * 512,
            sample_rate_hz=16_000,
            frame_count=512,
            adc_time_s=None,
            captured_monotonic_ns=sequence,
            discontinuity_before=sequence == 0,
            pcm16_mono=_pcm(sequence + 1),
        )
        for window, end_cursor in framer.feed(frame):
            windows.append(window)
            end_cursors.append(end_cursor)
    assert b"".join(windows) == source[: len(windows) * 1280 * 2]
    assert end_cursors == [1280, 2560, 3840, 5120]

    thresholds = voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        vad = voice_audio.SileroVad(mode="record")
        vad._t = thresholds
        assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=96,
                min_voiced_s=0.032,
                max_utterance_s=2.0,
            ),
            sample_rate_hz=16_000,
            frame_samples=512,
            session_id="S-pre-roll",
        )
        assembler.prepare()
        frames = [
            voice_audio.CanonicalAudioFrame(
                stream_epoch=1,
                sequence=index,
                sample_cursor=index * 512,
                sample_rate_hz=16_000,
                frame_count=512,
                adc_time_s=None,
                captured_monotonic_ns=index,
                discontinuity_before=index == 0,
                pcm16_mono=_pcm(value),
            )
            for index, value in enumerate(
                [0, 0, 0, 0, 0, 10_000, 11_000, 12_000, 0, 0],
            )
        ]
        for frame in frames[:4]:
            assembler.observe_idle(frame)
        assert assembler.arm(voice_session.WakeDetection(1, 2048, 0, 0.9)) == ()
        outcome: voice_session.CapturedUtterance | None = None
        for frame in frames[4:]:
            candidate = assembler.feed(frame)
            if isinstance(candidate, voice_session.CapturedUtterance):
                outcome = candidate
                break
        assert outcome is not None
        samples = np.frombuffer(outcome.audio_bytes, dtype="<i2")
        frame_markers = [int(samples[offset]) for offset in range(0, len(samples), 512)]
        assert frame_markers == [0, 10_000, 11_000, 12_000, 0, 0]
        assert outcome.end_sample_cursor - outcome.start_sample_cursor == len(samples)


def test_active_discontinuity_fails_current_audio_and_next_arm_restarts_clean() -> None:
    """A gap is never sent to ASR; a fresh wake can complete after reset."""
    thresholds = voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        vad = voice_audio.SileroVad(mode="record")
        vad._t = thresholds
        assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=32,
                min_voiced_s=0.032,
            ),
            sample_rate_hz=16_000,
            frame_samples=512,
            session_id="S-gap",
        )
        assembler.prepare()
        assembler.arm(voice_session.WakeDetection(1, 0, 0, 0.9))
        first = voice_audio.CanonicalAudioFrame(
            stream_epoch=1,
            sequence=0,
            sample_cursor=0,
            sample_rate_hz=16_000,
            frame_count=512,
            adc_time_s=None,
            captured_monotonic_ns=0,
            discontinuity_before=False,
            pcm16_mono=_pcm(10_000),
        )
        assert assembler.feed(first) is None
        gapped = replace(
            first,
            sequence=2,
            sample_cursor=1024,
            discontinuity_before=True,
        )
        failure = assembler.feed(gapped)
        assert isinstance(failure, voice_session.UtteranceCaptureFailure)
        assert failure.reason == "active_audio_discontinuity"

        assembler.prepare()
        assembler.arm(voice_session.WakeDetection(2, 0, 0, 0.9))
        second_round = [
            replace(
                first,
                stream_epoch=2,
                sequence=index,
                sample_cursor=index * 512,
                pcm16_mono=_pcm(value),
            )
            for index, value in enumerate([20_000, 21_000, 0, 0])
        ]
        committed = None
        for frame in second_round:
            candidate = assembler.feed(frame)
            if isinstance(candidate, voice_session.CapturedUtterance):
                committed = candidate
        assert committed is not None
        assert np.frombuffer(committed.audio_bytes, dtype="<i2")[0] == 20_000


def test_idle_overflow_recovers_and_active_overflow_marks_discontinuity() -> None:
    """Idle drops oldest/rebuilds; active capture receives an explicit gap marker."""
    backend = _FakeBackend()
    ingress = _ingress(backend, subscriber_capacity=2)
    capture = ingress.subscribe(
        name="small-capture",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
        capacity=2,
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch is not None
    for value in range(8):
        backend.emit(epoch=epoch, value=value)
    _wait_until(lambda: capture.overflow_count > 0)
    idle_frames = _read_frames(capture, 2)
    assert idle_frames[0].discontinuity_before
    assert [np.frombuffer(frame.pcm16_mono, dtype="<i2")[0] for frame in idle_frames] == [
        6,
        7,
    ]

    capture.set_active_utterance(active=True)
    for value in range(8, 14):
        backend.emit(epoch=epoch, value=value)
    _wait_until(lambda: capture.active_discontinuity_count > 0)
    active_frames = _read_frames(capture, 2)
    assert active_frames[0].discontinuity_before
    assert ingress.metrics().active_discontinuities > 0
    assert ingress.close().definitively_closed


def test_fault_reopen_sleep_wake_and_late_epoch_callback_rejection() -> None:
    """Every reopen resets profile/cursor and old callbacks are rejected."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    capture = ingress.subscribe(
        name="epoch-capture",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
    )
    assert ingress.start().started
    first_epoch = ingress.stream_epoch
    assert first_epoch == 1
    backend.emit(epoch=first_epoch, value=1)
    assert _read_frames(capture, 1)[0].sample_cursor == 0
    backend.pending_fault = voice_backend.BackendFault(
        stream_epoch=first_epoch,
        code="injected_device_loss",
        detail="integration",
        recoverable=True,
    )
    _wait_until(lambda: ingress.stream_epoch == 2)
    backend.emit(epoch=first_epoch, value=99)
    backend.emit(epoch=2, value=2)
    reopened = _read_frames(capture, 1)[0]
    assert reopened.stream_epoch == 2
    assert reopened.sample_cursor == 0
    assert ingress.metrics().late_epoch_callbacks_rejected == 1
    assert ingress.metrics().reopen_successes == 1

    sleep_result = ingress.stop_for_sleep()
    assert sleep_result is not None
    assert sleep_result.definitively_closed
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    wake_result = ingress.resume_after_wake()
    assert wake_result is not None
    assert wake_result.started
    assert ingress.stream_epoch == 3
    backend.emit(epoch=2, value=98)
    backend.emit(epoch=3, value=3)
    after_wake = _read_frames(capture, 1)[0]
    assert after_wake.stream_epoch == 3
    assert after_wake.sample_cursor == 0
    assert ingress.metrics().late_epoch_callbacks_rejected == 2
    assert ingress.close().definitively_closed
    assert backend.max_active_owner_count == 1


def test_device_fault_close_race_revokes_reopen_before_shutdown() -> None:
    """A close racing fault teardown cannot open a replacement epoch."""

    class _CoordinatedBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.stop_entered = threading.Event()
            self.release_stop = threading.Event()

        def stop(
            self,
            *,
            stream_epoch: int,
            attempt_id: str,
            timeout_s: float | None = None,
        ) -> voice_backend.BackendStopResult:
            del timeout_s
            self.stop_entered.set()
            assert self.release_stop.wait(timeout=0.5)
            return super().stop(stream_epoch=stream_epoch, attempt_id=attempt_id)

    backend = _CoordinatedBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    backend.pending_fault = voice_backend.BackendFault(
        stream_epoch=epoch,
        code="close_reopen_race",
        detail="integration",
        recoverable=True,
    )
    assert backend.stop_entered.wait(timeout=1.0)
    results: list[voice_audio.IngressCloseResult] = []
    closer = threading.Thread(target=lambda: results.append(ingress.close()))
    closer.start()
    _wait_until(lambda: ingress._closing)
    backend.release_stop.set()
    closer.join(timeout=1.0)
    assert not closer.is_alive()
    assert len(results) == 1
    assert results[0].definitively_closed
    assert backend.start_count == 1
    assert backend.stop_count == 1
    assert ingress.metrics().reopen_attempts == 0


def test_sleep_wins_real_fault_handler_backoff_without_post_sleep_reopen() -> None:
    """SUSPENDED remains final when sleep races an in-progress recovery loop."""
    backend = _FakeBackend()
    capabilities: list[voice_audio.InputCapabilitySnapshot] = []

    def _record_capability(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        capabilities.append(snapshot)

    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            reopen_initial_backoff_s=0.2,
            reopen_max_backoff_s=0.2,
            shutdown_timeout_s=0.5,
        ),
        capability_sink=_record_capability,
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    backend.pending_fault = voice_backend.BackendFault(
        stream_epoch=epoch,
        code="sleep_fault_race",
        detail="integration",
        recoverable=True,
        attempt_id=backend.active_attempt_id,
    )
    _wait_until(lambda: ingress.metrics().faults == 1 and backend.stop_count == 1)
    ingress.stop_for_sleep()
    time.sleep(0.25)
    assert backend.start_count == 1
    assert backend.max_active_owner_count == 1
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    versions = [snapshot.version for snapshot in capabilities]
    assert versions == sorted(set(versions))
    suspended_index = max(
        index
        for index, snapshot in enumerate(capabilities)
        if snapshot.state is voice_audio.InputCapabilityState.SUSPENDED
    )
    assert all(
        snapshot.state is voice_audio.InputCapabilityState.SUSPENDED
        for snapshot in capabilities[suspended_index:]
    )
    assert ingress.close().definitively_closed


def test_sleep_generation_revokes_fault_paused_at_physical_reopen_gate() -> None:
    """A sleep intent that wins the final CAS prevents a post-sleep open."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    before_open = threading.Event()
    release_open = threading.Event()
    original_open = ingress._open_new_epoch

    def _paused_open(
        *,
        reason: str,
        deadline: float | None = None,
        expected_control_generation: int | None = None,
    ) -> voice_backend.BackendStartResult:
        before_open.set()
        assert release_open.wait(timeout=1.0)
        return original_open(
            reason=reason,
            deadline=deadline,
            expected_control_generation=expected_control_generation,
        )

    fault = voice_backend.BackendFault(
        stream_epoch=epoch,
        code="sleep_before_physical_reopen",
        detail="integration",
        recoverable=True,
        attempt_id=backend.active_attempt_id,
    )
    with patch.object(ingress, "_open_new_epoch", side_effect=_paused_open):
        recovery = threading.Thread(target=lambda: ingress._handle_fault(fault))
        recovery.start()
        assert before_open.wait(timeout=1.0)
        slept = ingress.stop_for_sleep(deadline=time.monotonic() + 0.02)
        assert slept is not None
        release_open.set()
        recovery.join(timeout=1.0)
    assert not recovery.is_alive()
    assert backend.start_count == 1
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    assert ingress.close().definitively_closed


def test_sleep_generation_rejects_fault_terminal_publish_after_timeout() -> None:
    """A late fault terminal result cannot overwrite the SUSPENDED snapshot."""
    backend = _FakeBackend()
    capabilities: list[voice_audio.InputCapabilitySnapshot] = []

    def _capture_capability(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        capabilities.append(snapshot)

    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.001,
            shutdown_timeout_s=0.02,
            route_poll_s=60.0,
        ),
        capability_sink=_capture_capability,
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    before_publish = threading.Event()
    release_publish = threading.Event()
    original_publish = ingress._publish_capability_if_current

    def _paused_publish(
        generation: int,
        state: voice_audio.InputCapabilityState,
        *,
        reason: str,
    ) -> voice_audio.InputCapabilitySnapshot | None:
        if state is voice_audio.InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE:
            before_publish.set()
            assert release_publish.wait(timeout=1.0)
        return original_publish(generation, state, reason=reason)

    fault = voice_backend.BackendFault(
        stream_epoch=epoch,
        code="terminal_publish_after_sleep",
        detail="integration",
        recoverable=False,
        attempt_id=backend.active_attempt_id,
    )
    with patch.object(
        ingress,
        "_publish_capability_if_current",
        side_effect=_paused_publish,
    ):
        recovery = threading.Thread(target=lambda: ingress._handle_fault(fault))
        recovery.start()
        assert before_publish.wait(timeout=1.0)
        ingress.stop_for_sleep(deadline=time.monotonic() + 0.02)
        suspended_version = ingress.capability.version
        release_publish.set()
        recovery.join(timeout=1.0)
    assert not recovery.is_alive()
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    assert ingress.capability.version == suspended_version
    suspended_index = next(
        index
        for index, snapshot in enumerate(capabilities)
        if snapshot.version == suspended_version
    )
    assert all(
        snapshot.state is voice_audio.InputCapabilityState.SUSPENDED
        for snapshot in capabilities[suspended_index:]
    )
    assert ingress.close().definitively_closed


def test_foreign_reopen_returning_after_sleep_close_is_never_admitted() -> None:
    """Terminal control revokes an in-flight foreign open without lock inflation."""

    class _BlockedReopenBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.reopen_entered = threading.Event()
            self.reopen_release = threading.Event()

        def start(
            self,
            *,
            stream_epoch: int,
            attempt_id: str,
            frame_sink: voice_backend.InputFrameSink,
            render_source: voice_backend.RenderSource | None = None,
            timeout_s: float | None = None,
        ) -> voice_backend.BackendStartResult:
            if self.start_count == 1:
                self.reopen_entered.set()
                assert self.reopen_release.wait(timeout=1.0)
            return super().start(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                frame_sink=frame_sink,
                render_source=render_source,
                timeout_s=timeout_s,
            )

    backend = _BlockedReopenBackend()
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.001,
            shutdown_timeout_s=0.03,
            route_poll_s=60.0,
        ),
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch == 1
    fault = voice_backend.BackendFault(
        stream_epoch=epoch,
        code="blocked_foreign_reopen",
        detail="integration",
        recoverable=True,
        attempt_id=backend.active_attempt_id,
    )
    recovery = threading.Thread(target=lambda: ingress._handle_fault(fault))
    recovery.start()
    assert backend.reopen_entered.wait(timeout=1.0)
    ingress.stop_for_sleep(deadline=time.monotonic() + 0.02)
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    close_started = time.monotonic()
    first_close = ingress.close()
    close_wall_s = time.monotonic() - close_started
    assert close_wall_s < 0.08
    assert not first_close.definitively_closed
    assert ingress.stream_epoch is None
    terminal_version = ingress.capability.version
    assert ingress.capability.state.value == "close_uncertain"
    backend.reopen_release.set()
    recovery.join(timeout=1.0)
    assert not recovery.is_alive()
    assert ingress.stream_epoch is None
    assert backend.start_count == 2
    assert backend.stop_count == 2
    assert backend.max_active_owner_count == 1
    assert backend.active_epoch is None
    assert ingress.capability.version == terminal_version
    assert ingress.capability.state.value == "close_uncertain"
    assert ingress.close().definitively_closed
    assert ingress.capability.state.value == "stopped"
    stopped_version = ingress.capability.version
    ingress.report_output_unavailable(reason="late_power_callback_after_shutdown")
    assert ingress.capability.state.value == "stopped"
    assert ingress.capability.version == stopped_version


def test_blocked_fault_capability_observer_cannot_delay_close_generation() -> None:
    """Foreign observer code never owns control/capability shutdown locks."""
    observer_entered = threading.Event()
    observer_release = threading.Event()
    observed_versions: list[int] = []

    def _observe(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        observed_versions.append(snapshot.version)
        if snapshot.state is voice_audio.InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE:
            observer_entered.set()
            assert observer_release.wait(timeout=1.0)

    backend = _FakeBackend()
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            shutdown_timeout_s=0.04,
            route_poll_s=60.0,
        ),
        capability_sink=_observe,
    )
    assert ingress.start().started
    epoch = ingress.stream_epoch
    assert epoch is not None
    ingress._handle_fault(
        voice_backend.BackendFault(
            stream_epoch=epoch,
            code="nonrecoverable_observer_barrier",
            detail="integration",
            recoverable=False,
            attempt_id=backend.active_attempt_id,
        ),
    )
    assert observer_entered.wait(timeout=1.0)
    generation_before_close = ingress._control_generation
    started = time.monotonic()
    result = ingress.close()
    elapsed = time.monotonic() - started
    assert elapsed < 0.1
    assert result.definitively_closed
    assert ingress._control_generation > generation_before_close
    assert ingress.capability.state is voice_audio.InputCapabilityState.STOPPED
    observer_release.set()
    dispatcher = ingress._capability_dispatcher
    assert dispatcher is not None
    _wait_until(lambda: not dispatcher.thread_alive)
    assert observed_versions == sorted(observed_versions)
    assert observed_versions[-1] == ingress.capability.version


def test_blocked_stopped_capability_observer_is_outside_close_deadline() -> None:
    """A STOPPED notification may finish late without delaying typed close."""
    stopped_entered = threading.Event()
    stopped_release = threading.Event()
    observed_versions: list[int] = []

    def _observe(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        observed_versions.append(snapshot.version)
        if snapshot.state is voice_audio.InputCapabilityState.STOPPED:
            stopped_entered.set()
            assert stopped_release.wait(timeout=1.0)

    ingress = voice_audio.AudioIngress(
        backend=_FakeBackend(),
        config=replace(
            voice_audio.AudioIngressConfig(),
            shutdown_timeout_s=0.04,
            route_poll_s=60.0,
        ),
        capability_sink=_observe,
    )
    assert ingress.start().started
    started = time.monotonic()
    result = ingress.close()
    elapsed = time.monotonic() - started
    assert result.definitively_closed
    assert elapsed < 0.1
    assert stopped_entered.wait(timeout=1.0)
    stopped_release.set()
    dispatcher = ingress._capability_dispatcher
    assert dispatcher is not None
    _wait_until(lambda: not dispatcher.thread_alive)
    assert observed_versions == sorted(observed_versions)
    assert observed_versions[-1] == ingress.capability.version


def test_late_wake_report_cannot_overwrite_stopped_capability() -> None:
    """Wake-only capability CAS loses to a newer terminal generation."""
    report_checked = threading.Event()
    report_release = threading.Event()
    observed: list[voice_audio.InputCapabilitySnapshot] = []

    def _observe(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        observed.append(snapshot)

    ingress = voice_audio.AudioIngress(
        backend=_FakeBackend(),
        config=replace(
            voice_audio.AudioIngressConfig(),
            shutdown_timeout_s=0.03,
            route_poll_s=60.0,
        ),
        capability_sink=_observe,
    )
    assert ingress.start().started

    def _pause_report() -> None:
        report_checked.set()
        assert report_release.wait(timeout=1.0)

    ingress._wake_report_before_commit_hook = _pause_report
    report_results: list[voice_audio.InputCapabilitySnapshot] = []
    report = threading.Thread(
        target=lambda: report_results.append(
            ingress.report_wake_unavailable(reason="late_wake_health"),
        ),
    )
    report.start()
    assert report_checked.wait(timeout=1.0)
    closed = ingress.close()
    assert closed.definitively_closed
    stopped_version = ingress.capability.version
    assert ingress.capability.state is voice_audio.InputCapabilityState.STOPPED
    report_release.set()
    report.join(timeout=1.0)
    assert not report.is_alive()
    assert report_results[-1].state is voice_audio.InputCapabilityState.STOPPED
    assert ingress.capability.version == stopped_version
    dispatcher = ingress._capability_dispatcher
    assert dispatcher is not None
    _wait_until(lambda: not dispatcher.thread_alive)
    states = [snapshot.state for snapshot in observed]
    assert states[-1] is voice_audio.InputCapabilityState.STOPPED
    assert voice_audio.InputCapabilityState.WAKE_UNAVAILABLE not in states


def test_uncertain_close_keeps_capability_lane_until_definitive_retry() -> None:
    """CLOSE_UNCERTAIN never consumes the final STOPPED observer delivery."""
    backend = _FakeBackend(stop_status=voice_backend.BackendStopStatus.CLOSE_UNCERTAIN)
    observed: list[voice_audio.InputCapabilitySnapshot] = []

    def _observe(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        observed.append(snapshot)

    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            shutdown_timeout_s=0.03,
            route_poll_s=60.0,
        ),
        capability_sink=_observe,
    )
    assert ingress.start().started
    first = ingress.close()
    assert not first.definitively_closed
    assert ingress.capability.state is voice_audio.InputCapabilityState.CLOSE_UNCERTAIN
    dispatcher = ingress._capability_dispatcher
    assert dispatcher is not None
    assert dispatcher.thread_alive
    backend.stop_status = voice_backend.BackendStopStatus.CLOSED
    second = ingress.close()
    assert second.definitively_closed
    _wait_until(lambda: not dispatcher.thread_alive)
    assert [snapshot.version for snapshot in observed] == sorted(
        snapshot.version for snapshot in observed
    )
    assert observed[-1].state is voice_audio.InputCapabilityState.STOPPED
    assert dispatcher.dropped == 0


def test_duplex_session_runs_two_silero_utterances_without_second_round_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two wake/speech rounds reuse one prewarmed Silero with equal endpoints."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    wake_engine = _FakeWakeEngine(detections={0, 4})
    pipeline = _RecordingPipeline()
    silero_session = _EnergySession()
    with patch.object(voice_audio, "_load_silero_session", return_value=silero_session):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=wake_engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=2.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
            ),
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1

        # Each round spans two wake windows. Detection calls 0 and 4 are
        # separated far enough that final ASR + VAD re-prewarm have completed.
        for value in [0, 0, 0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: len(pipeline.calls) == 1)
        for value in [0, 0, 0, 0, 20_000, 21_000, 22_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: len(pipeline.calls) == 2)

        lengths = [len(call["audio_bytes"]) for call in pipeline.calls]
        assert lengths[0] == lengths[1]
        assert all(call["session_id"] for call in pipeline.calls)
        assert pipeline.calls[0]["utterance_id"] != pipeline.calls[1]["utterance_id"]
        assert all(call["endpoint_reason"] == "acoustic_pause" for call in pipeline.calls)
        metrics = session.metrics()
        assert metrics.endpoint_commits == 2
        assert metrics.capture_discontinuities == 0
        close = session.close()
    assert close.definitively_closed
    assert wake_engine.closed
    assert not close.alive_threads
    reset_indices = [
        index for index, value in enumerate(silero_session.input_h_values) if value == 0.0
    ]
    c_reset_indices = [
        index for index, value in enumerate(silero_session.input_c_values) if value == 0.0
    ]
    assert len(reset_indices) >= 3
    assert c_reset_indices == reset_indices
    for reset_index in reset_indices[:3]:
        assert silero_session.input_h_values[reset_index : reset_index + 5] == [
            0.0,
            1.0,
            2.0,
            3.0,
            4.0,
        ]
        assert silero_session.input_c_values[reset_index : reset_index + 5] == [
            0.0,
            10.0,
            20.0,
            30.0,
            40.0,
        ]


def test_output_active_keeps_sampling_but_cannot_cancel_or_commit_echo() -> None:
    """Wave 3 suppresses wake decisions during output and has no cancel callable."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    engine = _FakeWakeEngine()
    pipeline = _RecordingPipeline()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: True,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
            ),
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().wake_suppressed_during_output > 0)
        assert ingress.metrics().callback_calls == 12
        assert pipeline.calls == []
        assert session.metrics().wake_detections > 0
        assert session.close().definitively_closed


def test_armed_barge_in_in_ptt_mode_still_suppresses_the_wake_hit() -> None:
    """ADR-0006 D9: only PTT may confirm on a ptt-ceiling route.

    Barge-in is configured on and an interrupt callable is injected, so the
    only thing keeping the wake hit inert is the mode table.  ``output_active``
    alone and a wake hit alone still cancel nothing and arm nothing.
    """
    backend = _FakeBackend()
    ingress = _ingress(backend)
    engine = _FakeWakeEngine()
    pipeline = _RecordingPipeline()
    interrupts: list[str] = []

    def _interrupt(source: str) -> str:
        interrupts.append(source)
        return "cancelled"

    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: True,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
                barge_in=voice_interrupt.BargeInConfig(enabled=True),
            ),
            barge_in_interrupt=_interrupt,
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().wake_suppressed_during_output > 0)
        metrics = session.metrics()
        profile = session.device_profile
        assert session.close().definitively_closed

    assert profile is not None
    assert profile.allowed_barge_mode == "ptt"
    assert metrics.barge_in_candidates == 0
    assert metrics.barge_in_confirmations == 0
    assert metrics.capture_starts == 0
    assert interrupts == []
    assert pipeline.calls == []


def test_wake_prediction_failure_downgrades_only_wake_then_recovers() -> None:
    """Wake model faults retain input/PTT/text and recover after a real score."""

    class _RecoveringWakeEngine(_FakeWakeEngine):
        def predict(self, frame_bytes: bytes) -> dict[str, float]:
            if self.calls < 3:
                self.calls += 1
                raise RuntimeError
            return super().predict(frame_bytes)

    backend = _FakeBackend()
    capabilities: list[voice_audio.InputCapabilitySnapshot] = []

    def _record_capability(snapshot: voice_audio.InputCapabilitySnapshot) -> None:
        capabilities.append(snapshot)

    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            worker_poll_s=0.0005,
            route_poll_s=60.0,
            shutdown_timeout_s=1.0,
        ),
        capability_sink=_record_capability,
    )
    engine = _RecoveringWakeEngine(set())
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=_RecordingPipeline(),
            broadcaster=None,
            output_active=None,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                wake_failure_threshold=3,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
            ),
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
            time.sleep(0.002)
        _wait_until(lambda: session.metrics().wake_prediction_failures == 3)
        _wait_until(lambda: ingress.capability.state is voice_audio.InputCapabilityState.AVAILABLE)
        assert any(
            snapshot.state is voice_audio.InputCapabilityState.WAKE_UNAVAILABLE
            and not snapshot.wake_available
            and snapshot.local_capture_available
            and snapshot.ptt_upload_available
            and snapshot.text_available
            for snapshot in capabilities
        )
        assert session.close().definitively_closed


def test_stuck_backend_and_full_commit_queue_are_bounded_and_auditable() -> None:
    """Stuck close never authorizes reopen; full final-ASR queue rejects newest."""
    backend = _FakeBackend(stop_status=voice_backend.BackendStopStatus.CLOSE_UNCERTAIN)
    ingress = _ingress(backend)
    subscription = ingress.subscribe(
        name="stuck-capture",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
    )
    assert ingress.start().started
    result = ingress.close()
    assert not result.definitively_closed
    assert result.backend_result is not None
    assert result.backend_result.status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
    assert not result.worker_alive
    assert result.open_subscribers == 0
    assert backend.start_count == 1
    assert subscription.read(timeout_s=0.001) is None

    healthy_backend = _FakeBackend()
    second_ingress = _ingress(healthy_backend)
    pipeline = _RecordingPipeline()
    engine = _FakeWakeEngine(set())
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=second_ingress,
            wake_engine=engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=None,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                commit_queue_capacity=1,
                shutdown_timeout_s=1.0,
            ),
        )
        first = voice_session.CapturedUtterance(
            "S",
            "U1",
            "T1",
            1,
            0,
            512,
            "acoustic_pause",
            _pcm(1),
        )
        second = replace(first, utterance_id="U2", turn_id="T2")
        session._handle_capture_outcome(first)
        session._handle_capture_outcome(second)
        assert session.metrics().commit_queue_full == 1
        assert session.close().definitively_closed


def test_feature_off_on_and_dependency_downgrade_are_explicit() -> None:
    """Feature-off is legacy; flag-on requires accepted Wave-1/2 capability."""
    runtime = MagicMock()
    runtime.config = {"realtime": {"enabled": False}}
    runtime.wave1_features = Wave1FeatureFlags()
    off = inherent_loop._single_ingress_activation(runtime, tts=None)
    assert not off.requested
    assert off.reason == "feature_disabled"

    runtime.config = {
        "realtime": {
            "enabled": True,
            "single_audio_ingress": {"enabled": True},
            "streaming_output": {"enabled": True},
        },
    }
    missing = inherent_loop._single_ingress_activation(runtime, tts=None)
    assert missing.requested
    assert not missing.capable
    assert missing.reason == "wave1_capability_missing"

    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    wave2 = object.__new__(voice_media.StreamingTTSPipeline)
    enabled = inherent_loop._single_ingress_activation(runtime, tts=wave2)
    assert enabled.requested
    assert enabled.capable
    assert enabled.ingress_config is not None
    assert enabled.session_config is not None

    runtime.config["realtime"]["single_audio_ingress"]["canonical_frame_samples"] = 480
    invalid = inherent_loop._single_ingress_activation(runtime, tts=wave2)
    assert not invalid.capable
    assert invalid.reason.startswith("invalid_input_config:")


def test_voice_pipeline_build_is_feature_off_cold_and_explicit_prewarm_is_available(
    tmp_path: Path,
) -> None:
    """Legacy feature-off construction keeps its prior lazy-ASR semantics."""
    runtime = MagicMock()
    runtime.runtime_paths.event_log = tmp_path / "events.db"
    runtime.runtime_paths.artifacts_root = tmp_path / "artifacts"
    recognizer = MagicMock(spec=voice_asr.SenseVoiceRecognizer)
    with patch.object(voice_asr, "SenseVoiceRecognizer", return_value=recognizer):
        pipeline = inherent_loop._build_voice_pipeline(
            runtime,
            broadcaster=MagicMock(),
            sensevoice_dir=tmp_path / "sensevoice",
        )
    recognizer.prewarm.assert_not_called()
    assert pipeline._recognizer is recognizer
    pipeline.prewarm_input_model()
    recognizer.prewarm.assert_called_once_with()


def test_real_concurrent_churn_1000_epoch_subscribe_fanout_close_cycles() -> None:  # noqa: PLR0915
    """1,000 real thread/callback cycles show no wrap, epoch, deadlock, or leak."""
    backend = _FakeBackend()
    ingress = _ingress(backend, native_capacity=8, subscriber_capacity=2)
    assert ingress.start().started
    retained: list[bytes] = []
    for cycle in range(1_000):
        epoch = ingress.stream_epoch
        assert epoch is not None
        old_timeline = ingress._active_timeline
        assert old_timeline is not None
        entered = threading.Event()
        release = threading.Event()

        original_commit = old_timeline.native_ring._commit_slot

        def _pause_after_final_validation(
            slot: object,
            write_index: int,
            entered_event: threading.Event = entered,
            release_event: threading.Event = release,
            commit: Callable[[Any, int], None] = original_commit,
        ) -> None:
            entered_event.set()
            assert release_event.wait(timeout=1.0)
            commit(slot, write_index)

        old_timeline.native_ring._commit_slot = _pause_after_final_validation  # type: ignore[method-assign]
        stale_callback = threading.Thread(
            target=lambda old_epoch=epoch: backend.emit(
                epoch=old_epoch,
                value=31_000,
            ),
            name="wave3-stale-callback-barrier",
        )
        stale_callback.start()
        assert entered.wait(timeout=1.0)
        stopped = ingress.stop_for_sleep()
        assert stopped is not None
        assert stopped.definitively_closed
        reopened = ingress.resume_after_wake()
        assert reopened is not None
        assert reopened.started
        successor = ingress.subscribe(
            name=f"churn-{cycle}",
            purpose=voice_audio.SubscriberPurpose.CAPTURE,
            capacity=2,
        )
        successor_epoch = ingress.stream_epoch
        assert successor_epoch is not None
        assert successor_epoch != epoch
        backend.emit(epoch=successor_epoch, value=cycle % 30_000)
        frame = _read_frames(successor, 1)[0]
        assert frame.stream_epoch == successor_epoch
        assert frame.sample_cursor == 0
        assert frame.pcm16_mono == _pcm(cycle % 30_000)
        if cycle < 8:
            retained.append(frame.pcm16_mono)
        successor.close()
        release.set()
        stale_callback.join(timeout=1.0)
        assert not stale_callback.is_alive()
    close = ingress.close()
    assert close.definitively_closed
    assert not close.worker_alive
    assert close.open_subscribers == 0
    assert backend.max_active_owner_count == 1
    assert backend.start_count == 1_001
    assert backend.stop_count == 1_001
    assert ingress.metrics().late_epoch_callbacks_rejected == 1_000
    assert retained == [_pcm(index) for index in range(8)]
    assert not any(
        thread.name.startswith("jarvis-audio-ingress") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_false_wake_armed_timeout_requires_a_fresh_wake_for_later_speech() -> None:
    """Silence expires ARMED identity; unrelated future speech cannot commit."""
    thresholds = voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        vad = voice_audio.SileroVad(mode="record")
        vad._t = thresholds
        assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                armed_no_speech_timeout_s=0.064,
                pre_roll_ms=32,
                min_voiced_s=0.032,
            ),
            sample_rate_hz=16_000,
            frame_samples=512,
            session_id="S-armed-timeout",
        )
        assembler.prepare()
        assembler.arm(voice_session.WakeDetection(1, 0, 0, 0.9))

        def _frame(index: int, value: int) -> voice_audio.CanonicalAudioFrame:
            return voice_audio.CanonicalAudioFrame(
                stream_epoch=1,
                sequence=index,
                sample_cursor=index * 512,
                sample_rate_hz=16_000,
                frame_count=512,
                adc_time_s=None,
                captured_monotonic_ns=index,
                discontinuity_before=False,
                pcm16_mono=_pcm(value),
            )

        assert assembler.feed(_frame(0, 0)) is None
        expired = assembler.feed(_frame(1, 0))
        assert isinstance(expired, voice_session.WakeArmExpired)
        assert not assembler.armed
        assert assembler.feed(_frame(2, 20_000)) is None
        assert not assembler.armed

        assembler.arm(voice_session.WakeDetection(1, 1536, 3, 0.9))
        committed = None
        for index, value in enumerate([20_000, 21_000, 0, 0], start=3):
            outcome = assembler.feed(_frame(index, value))
            if isinstance(outcome, voice_session.CapturedUtterance):
                committed = outcome
        assert committed is not None
        assert committed.start_sample_cursor >= 1536


def test_old_callback_paused_before_ring_commit_cannot_contaminate_new_epoch() -> None:
    """The ring's exact publication token is revalidated after slot copy."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    capture = ingress.subscribe(
        name="publication-race",
        purpose=voice_audio.SubscriberPurpose.CAPTURE,
    )
    assert ingress.start().started
    first_epoch = ingress.stream_epoch
    assert first_epoch == 1
    first_timeline = ingress._active_timeline
    assert first_timeline is not None
    entered = threading.Event()
    release = threading.Event()
    blocked_once = False

    original_commit = first_timeline.native_ring._commit_slot

    def _pause_after_final_validation(slot: object, write_index: int) -> None:
        nonlocal blocked_once
        if not blocked_once:
            blocked_once = True
            entered.set()
            assert release.wait(timeout=1.0)
        original_commit(slot, write_index)  # type: ignore[arg-type]

    first_timeline.native_ring._commit_slot = _pause_after_final_validation  # type: ignore[method-assign]
    old_callback = threading.Thread(
        target=lambda: backend.emit(epoch=first_epoch, value=111),
        name="paused-old-epoch-callback",
    )
    old_callback.start()
    assert entered.wait(timeout=1.0)
    assert ingress.stop_for_sleep() is not None
    reopened = ingress.resume_after_wake()
    assert reopened is not None
    assert reopened.started
    assert ingress.stream_epoch == 2
    release.set()
    old_callback.join(timeout=1.0)
    assert not old_callback.is_alive()
    backend.emit(epoch=2, value=222)
    frame = _read_frames(capture, 1)[0]
    assert frame.stream_epoch == 2
    assert frame.sample_cursor == 0
    assert not frame.discontinuity_before
    assert int(np.frombuffer(frame.pcm16_mono, dtype="<i2")[0]) == 222
    assert ingress.metrics().late_epoch_callbacks_rejected == 1
    assert ingress.close().definitively_closed


def test_subscribe_insert_racing_reopen_receives_successor_epoch() -> None:
    """Membership and token publication share one lock across epoch commit."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    entered = threading.Event()
    release = threading.Event()

    def _pause_before_insert() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    ingress._subscribe_before_insert_hook = _pause_before_insert
    subscriptions: list[voice_audio.AudioSubscription] = []
    subscriber_thread = threading.Thread(
        target=lambda: subscriptions.append(
            ingress.subscribe(
                name="subscribe-reopen-race",
                purpose=voice_audio.SubscriberPurpose.CAPTURE,
            ),
        ),
    )
    subscriber_thread.start()
    assert entered.wait(timeout=1.0)
    power_results: list[object] = []

    def _cycle_power() -> None:
        power_results.append(ingress.stop_for_sleep())
        power_results.append(ingress.resume_after_wake())

    power_thread = threading.Thread(target=_cycle_power)
    power_thread.start()
    release.set()
    subscriber_thread.join(timeout=1.0)
    power_thread.join(timeout=1.0)
    assert not subscriber_thread.is_alive()
    assert not power_thread.is_alive()
    assert len(power_results) == 2
    assert len(subscriptions) == 1
    assert ingress.stream_epoch == 2
    backend.emit(epoch=2, value=222)
    frame = _read_frames(subscriptions[0], 1)[0]
    assert frame.stream_epoch == 2
    subscriptions[0].close()
    assert ingress.close().definitively_closed


def test_subscribe_insert_racing_close_returns_no_live_membership() -> None:
    """Close marks the gate and drains membership atomically with subscribe."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    entered = threading.Event()
    release = threading.Event()

    def _pause_before_insert() -> None:
        entered.set()
        assert release.wait(timeout=1.0)

    ingress._subscribe_before_insert_hook = _pause_before_insert
    subscriptions: list[voice_audio.AudioSubscription] = []
    subscriber_thread = threading.Thread(
        target=lambda: subscriptions.append(
            ingress.subscribe(
                name="subscribe-close-race",
                purpose=voice_audio.SubscriberPurpose.DIAGNOSTIC,
            ),
        ),
    )
    subscriber_thread.start()
    assert entered.wait(timeout=1.0)
    close_results: list[voice_audio.IngressCloseResult] = []
    close_thread = threading.Thread(target=lambda: close_results.append(ingress.close()))
    close_thread.start()
    release.set()
    subscriber_thread.join(timeout=1.0)
    close_thread.join(timeout=1.0)
    assert not subscriber_thread.is_alive()
    assert not close_thread.is_alive()
    assert len(subscriptions) == 1
    assert subscriptions[0].read(timeout_s=0.0) is None
    assert close_results[0].definitively_closed
    assert close_results[0].open_subscribers == 0
    assert ingress._subscriber_snapshot == ()


def test_ring_overflow_vs_consumer_commit_1000_cycles_marks_first_post_gap() -> None:
    """Producer watermark wins every final-cursor race without writing read_index."""
    ring = voice_audio._PreallocatedPcmRing(capacity=1, max_frame_bytes=1024)

    def _write(sequence: int, value: int) -> bool:
        return ring.write(
            pcm=_pcm(value),
            byte_count=1024,
            stream_epoch=1,
            sequence=sequence,
            sample_cursor=sequence * 512,
            sample_rate_hz=16_000,
            channels=1,
            frame_count=512,
            adc_time_s=None,
            captured_monotonic_ns=sequence,
            discontinuity_before=False,
            purpose=voice_audio.SubscriberPurpose.WAKE,
            active=False,
        )

    for cycle in range(1_000):
        sequence = cycle * 2
        assert _write(sequence, cycle)
        entered = threading.Event()
        release = threading.Event()

        def _pause(
            _expected_index: int,
            entered_event: threading.Event = entered,
            release_event: threading.Event = release,
        ) -> None:
            entered_event.set()
            assert release_event.wait(timeout=1.0)

        ring._consumer_before_commit_hook = _pause
        raced: list[voice_audio._OwnedPcmFrame | None] = []
        def _consume(
            target: list[voice_audio._OwnedPcmFrame | None] = raced,
        ) -> None:
            target.append(ring.read())

        consumer = threading.Thread(target=_consume)
        consumer.start()
        assert entered.wait(timeout=1.0)
        read_before_overflow = ring._read_index
        assert _write(sequence + 1, cycle + 1)
        assert ring._read_index == read_before_overflow
        ring._consumer_before_commit_hook = None
        release.set()
        consumer.join(timeout=1.0)
        assert not consumer.is_alive()
        assert raced == [None]
        post_gap = ring.read()
        assert post_gap is not None
        assert post_gap.sequence == sequence + 1
        assert post_gap.discontinuity_before
    assert ring.overflow_count == 1_000


def test_backend_same_instance_open_close_attempts_are_serial_and_monotonic() -> None:
    """Concurrent same-backend start/close joins one exact physical attempt."""

    class _BlockingStream:
        active = False

        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            self.active = True

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            self.close_calls += 1
            close_entered.set()
            assert close_release.wait(timeout=1.0)

    open_entered = threading.Event()
    open_release = threading.Event()
    close_entered = threading.Event()
    close_release = threading.Event()
    streams: list[_BlockingStream] = []

    def _open(**_kwargs: object) -> _BlockingStream:
        open_entered.set()
        assert open_release.wait(timeout=1.0)
        stream = _BlockingStream()
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "serial-device",
        "serial-device",
        "sounddevice",
        input_format,
    )
    backend = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.5,
        close_timeout_s=0.01,
    )
    starts: list[voice_backend.BackendStartResult] = []
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        opener = threading.Thread(
            target=lambda: starts.append(
                backend.start(stream_epoch=1, attempt_id="A", frame_sink=MagicMock()),
            ),
        )
        opener.start()
        assert open_entered.wait(timeout=1.0)
        concurrent = backend.start(
            stream_epoch=2,
            attempt_id="B",
            frame_sink=MagicMock(),
        )
        assert concurrent.status is voice_backend.BackendStartStatus.OWNER_BUSY
        open_release.set()
        opener.join(timeout=1.0)
        assert starts
        assert starts[0].started

        closes: list[voice_backend.BackendStopResult] = []
        first_closer = threading.Thread(
            target=lambda: closes.append(backend.stop(stream_epoch=1, attempt_id="A")),
        )
        second_closer = threading.Thread(
            target=lambda: closes.append(backend.stop(stream_epoch=1, attempt_id="A")),
        )
        first_closer.start()
        assert close_entered.wait(timeout=1.0)
        second_closer.start()
        busy = backend.start(stream_epoch=3, attempt_id="C", frame_sink=MagicMock())
        assert busy.status is voice_backend.BackendStartStatus.OWNER_BUSY
        first_closer.join(timeout=1.0)
        second_closer.join(timeout=1.0)
        assert len(closes) == 2
        assert all(
            result.status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
            for result in closes
        )
        assert all(not result.definitively_closed for result in closes)
        assert streams[0].close_calls == 1
        close_release.set()
        _wait_until(
            lambda: backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED,
        )
        replay = backend.stop(stream_epoch=1, attempt_id="A")
        assert replay.definitively_closed
    assert len(streams) == 1


@pytest.mark.parametrize("reopen_source", ["fault", "wake"])
def test_open_timeout_recovery_debt_survives_ingress_close(
    reopen_source: str,
) -> None:
    """Fault/wake open timeout remains close debt until its exact helper settles."""
    owner_counts = [0, 0]  # active, lifetime maximum

    class _CountingStream:
        active = False
        closed = False

        def start(self) -> None:
            self.active = True
            owner_counts[0] += 1
            owner_counts[1] = max(owner_counts[1], owner_counts[0])

        def abort(self) -> None:
            self.active = False

        def close(self) -> None:
            if not self.closed:
                self.closed = True
                owner_counts[0] -= 1

    second_open_entered = threading.Event()
    release_second_open = threading.Event()
    streams: list[_CountingStream] = []

    def _open(**_kwargs: object) -> _CountingStream:
        if streams:
            second_open_entered.set()
            assert release_second_open.wait(timeout=1.0)
        stream = _CountingStream()
        streams.append(stream)
        return stream

    input_format = voice_backend.AudioInputFormat(16_000, 1, 512)
    profile = voice_backend.InputDeviceProfile(
        "timeout-recovery-device",
        "timeout-recovery-device",
        "sounddevice",
        input_format,
    )
    backend = voice_backend.SoundDeviceDuplexBackend(
        input_format=input_format,
        open_timeout_s=0.01,
        close_timeout_s=0.01,
    )
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.001,
            shutdown_timeout_s=0.02,
            route_poll_s=60.0,
        ),
    )
    with (
        patch.object(voice_backend, "_open_sounddevice_input_stream", side_effect=_open),
        patch.object(voice_backend, "_default_input_device_profile", return_value=profile),
    ):
        assert ingress.start().started
        if reopen_source == "fault":
            ingress.notify_route_change(reason="injected_fault_reopen")
        else:
            stopped = ingress.stop_for_sleep()
            assert stopped is not None
            assert stopped.definitively_closed
            wake = ingress.resume_after_wake()
            assert wake is not None
            assert wake.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
        assert second_open_entered.is_set()
        debt = backend.ownership_snapshot()
        assert debt.state is voice_backend.BackendLifecycleState.UNCERTAIN
        first_close = ingress.close()
        second_close = ingress.close()
        assert not first_close.definitively_closed
        assert not second_close.definitively_closed
        assert first_close.backend_result is not None
        assert second_close.backend_result is not None
        release_second_open.set()
        _wait_until(
            lambda: backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED,
        )
        assert ingress.close().definitively_closed
    assert len(streams) == 2
    assert owner_counts == [0, 1]


def test_sleep_lock_timeout_wake_and_close_never_overwrite_physical_debt() -> None:
    """Ingress lock timeout still closes backend debt and forbids a second open."""
    backend = _FakeBackend()
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            shutdown_timeout_s=0.01,
            route_poll_s=60.0,
        ),
    )
    assert ingress.start().started
    ingress._lifecycle_lock.acquire()
    try:
        slept = ingress.stop_for_sleep()
        assert slept is not None
        assert slept.definitively_closed
        wake = ingress.resume_after_wake()
        assert wake is not None
        assert wake.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
        closed = ingress.close()
    finally:
        ingress._lifecycle_lock.release()
    assert closed.definitively_closed
    assert backend.start_count == 1
    assert backend.max_active_owner_count == 1


def test_duplex_partial_thread_start_failure_closes_started_owners() -> None:
    """A later Thread.start failure joins only threads that actually started."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    wake_engine = _FakeWakeEngine(set())
    session = voice_session.DuplexVoiceSession(
        ingress=ingress,
        wake_engine=wake_engine,
        vad=MagicMock(spec=voice_audio.SileroVad),
        pipeline=_RecordingPipeline(),
        broadcaster=None,
        output_active=None,
        wake_threshold=0.5,
        config=replace(
            voice_session.RealtimeInputSessionConfig(),
            shutdown_timeout_s=0.5,
        ),
    )
    session._assembler.prepare = MagicMock()  # type: ignore[method-assign]
    original_start = threading.Thread.start

    def _start(thread: threading.Thread) -> None:
        if thread.name == "jarvis-utterance-ingress":
            message = "injected partial start"
            raise RuntimeError(message)
        original_start(thread)

    with (
        patch.object(threading.Thread, "start", _start),
        pytest.raises(RuntimeError, match="injected partial start"),
    ):
        session.start()
    assert wake_engine.closed
    assert not any(thread.is_alive() for thread in session._started_threads)
    assert backend.active_owner_count == 0


def test_correlated_endpoint_candidate_is_reportable_by_turn() -> None:
    """Assembler endpoint and gap-free commit share turn/session identity."""
    reset_realtime_trace()
    thresholds = voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2)
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        vad = voice_audio.SileroVad(mode="record")
        vad._t = thresholds
        assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                min_voiced_s=0.032,
            ),
            sample_rate_hz=16_000,
            frame_samples=512,
            session_id="S-report",
        )
        assembler.prepare()
        assembler.arm(voice_session.WakeDetection(1, 0, 0, 0.9))
        outcome = None
        for index, value in enumerate([20_000, 20_000, 0, 0]):
            outcome = assembler.feed(
                voice_audio.CanonicalAudioFrame(
                    stream_epoch=1,
                    sequence=index,
                    sample_cursor=index * 512,
                    sample_rate_hz=16_000,
                    frame_count=512,
                    adc_time_s=None,
                    captured_monotonic_ns=index,
                    discontinuity_before=False,
                    pcm16_mono=_pcm(value),
                ),
            )
        assert isinstance(outcome, voice_session.CapturedUtterance)
        backend = _FakeBackend()
        session = voice_session.DuplexVoiceSession(
            ingress=_ingress(backend),
            wake_engine=_FakeWakeEngine(set()),
            vad=MagicMock(spec=voice_audio.SileroVad),
            pipeline=_RecordingPipeline(),
            broadcaster=None,
            output_active=None,
            wake_threshold=0.5,
            config=voice_session.RealtimeInputSessionConfig(),
        )
        session._assembler.prepare = MagicMock()  # type: ignore[method-assign]
        session._handle_capture_outcome(outcome)
        points = realtime_trace_snapshot()
        endpoint = next(point for point in points if point.name == "endpoint_candidate")
        assert endpoint.attributes["turn_id"] == outcome.turn_id
        rows = tuple(
            TraceRow(point.name, point.monotonic_ns, dict(point.attributes))
            for point in points
        )
        report = summarize_trace(rows, scenario="live_voice", turn_id=outcome.turn_id)
        assert "endpoint_candidate_to_gap_free_audio_ms" in report["durations_ms"]
        assert session.close().definitively_closed


@pytest.mark.parametrize(
    ("git_state", "expected_revision", "expected_reason"),
    [
        ({"head": "actual", "dirty": False}, "expected", "revision_mismatch"),
        ({"head": None, "dirty": False}, "expected", "git_head_unknown"),
        ({"head": "actual", "dirty": True}, "actual", "worktree_dirty_or_unknown"),
    ],
)
def test_input_bench_ineligible_provenance_fails_before_device_work(
    tmp_path: Path,
    git_state: dict[str, object],
    expected_revision: str,
    expected_reason: str,
) -> None:
    """Bench rejects mismatch, unknown, or dirty state before sounddevice import."""
    output = tmp_path / "ineligible.json"
    with (
        patch.object(
            voice_input_bench,
            "_git_provenance",
            return_value=git_state,
        ),
        patch.object(
            voice_input_bench,
            "_base_report",
            side_effect=AssertionError("device/provider work must not begin"),
        ),
    ):
        code = voice_input_bench.main(
            [
                "--synthetic-churn-cycles",
                "1",
                "--expected-revision",
                expected_revision,
                "--output",
                str(output),
            ],
        )
    report = json.loads(output.read_text())
    assert code == 2
    assert report["status"] == "INELIGIBLE"
    assert report["eligibility"]["reasons"] == [expected_reason]


def test_input_bench_synthetic_status_requires_every_exact_churn_gate() -> None:
    """The synthetic report cannot say observed with a missed stale callback."""
    report = voice_input_bench.run_synthetic_churn(cycles=25)
    assert report["status"] == "observed"
    assert report["cycles_completed"] == 25
    assert report["late_epoch_callbacks_rejected"] == 25
    strict_checks = report["strict_checks"]
    assert isinstance(strict_checks, dict)
    assert all(strict_checks.values())


def test_live_bench_tail_drain_waits_past_initial_empty_worker_poll() -> None:
    """One early empty read cannot hide an accepted delayed native tail."""
    frame = voice_audio.CanonicalAudioFrame(
        stream_epoch=1,
        sequence=0,
        sample_cursor=0,
        sample_rate_hz=16_000,
        frame_count=512,
        adc_time_s=None,
        captured_monotonic_ns=1,
        discontinuity_before=False,
        pcm16_mono=_pcm(7),
    )
    subscriber = MagicMock(spec=voice_audio.AudioSubscription)
    subscriber.read.side_effect = [None, frame, None, None]
    delayed = MagicMock()
    delayed.callback_calls = 1
    delayed.canonical_frames = 0
    converged = MagicMock()
    converged.callback_calls = 1
    converged.canonical_frames = 1
    ingress = MagicMock(spec=voice_audio.AudioIngress)
    ingress.metrics.side_effect = [delayed, converged, converged]
    frames: list[voice_audio.CanonicalAudioFrame] = []
    completed, stable_polls, deadline_exhausted = (
        voice_input_bench._drain_accepted_tail(
        ingress=ingress,
        subscriber=subscriber,
        frames=frames,
        deadline=time.monotonic() + 0.2,
        )
    )
    assert completed
    assert not deadline_exhausted
    assert stable_polls == 2
    assert frames == [frame]
    assert subscriber.read.call_count == 4


def test_live_bench_second_stable_poll_crossing_deadline_fails_closed() -> None:
    """Count convergence after the absolute deadline is never a passing drain."""
    subscriber = MagicMock(spec=voice_audio.AudioSubscription)
    reads = 0

    def _read(*, timeout_s: float) -> None:
        nonlocal reads
        reads += 1
        if reads == 2:
            time.sleep(timeout_s + 0.01)

    subscriber.read.side_effect = _read
    converged = MagicMock()
    converged.callback_calls = 0
    converged.canonical_frames = 0
    ingress = MagicMock(spec=voice_audio.AudioIngress)
    ingress.metrics.return_value = converged
    completed, stable_polls, deadline_exhausted = (
        voice_input_bench._drain_accepted_tail(
            ingress=ingress,
            subscriber=subscriber,
            frames=[],
            deadline=time.monotonic() + 0.015,
        )
    )
    assert not completed
    assert deadline_exhausted
    assert stable_polls == 1
    assert reads == 2


def test_power_coordinator_orders_input_then_output_and_fresh_output_before_input() -> None:
    """Composition root owns the ADR sleep/wake ordering across both devices."""
    actions: list[str] = []
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.side_effect = lambda **_kwargs: actions.append(
        "input_stop",
    )
    session.ingress.resume_after_wake.side_effect = lambda **_kwargs: actions.append(
        "input_start",
    )
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)

    def _stop_output(**_kwargs: object) -> voice_media.MediaPowerTransitionResult:
        actions.append("output_stop")
        return voice_media.MediaPowerTransitionResult("suspended", 1, "closed")

    def _start_output(**_kwargs: object) -> voice_media.MediaPowerTransitionResult:
        actions.append("output_start")
        return voice_media.MediaPowerTransitionResult("resumed", 2, "opened")

    media.suspend_for_sleep.side_effect = _stop_output
    media.resume_after_wake.side_effect = _start_output
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    coordinator.before_sleep()
    coordinator.on_wake()
    assert actions == ["input_stop", "output_stop", "output_start", "input_start"]


@pytest.mark.parametrize(
    "status",
    [
        voice_backend.BackendStartStatus.STARTED,
        voice_backend.BackendStartStatus.FAILED_CLOSED,
        voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
        voice_backend.BackendStartStatus.OWNER_BUSY,
        None,
    ],
)
def test_power_coordinator_admits_output_only_after_typed_input_start(
    status: voice_backend.BackendStartStatus | None,
) -> None:
    """Non-None typed input failures never masquerade as resumed ownership."""
    expected_admit = status is voice_backend.BackendStartStatus.STARTED
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    if status is None:
        input_result: object | None = None
    else:
        input_result = voice_backend.BackendStartResult(
            status=status,
            stream_epoch=2,
            profile=None,
            reason=status.value,
            attempt_id="typed-input-a2",
        )
    session.ingress.resume_after_wake.return_value = input_result
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)
    media.resume_after_wake.return_value = voice_media.MediaPowerTransitionResult(
        "resumed",
        81,
        "fresh_output",
    )
    media.admit_wake_start.return_value = True
    media.abort_wake_start.return_value = voice_media.MediaPowerTransitionResult(
        "suspended",
        81,
        "input_failure_aborted_output",
    )
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    result = coordinator.on_wake()
    if expected_admit:
        assert result.input_skipped_reason is None
        media.admit_wake_start.assert_called_once_with(attempt_id=81)
        media.abort_wake_start.assert_not_called()
        session.ingress.stop_for_sleep.assert_not_called()
    else:
        assert result.input_skipped_reason is not None
        media.admit_wake_start.assert_not_called()
        media.abort_wake_start.assert_called_once()
        if input_result is None:
            session.ingress.stop_for_sleep.assert_not_called()
        else:
            session.ingress.stop_for_sleep.assert_called_once()
    media.revoke_wake_starts_for_shutdown.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [
        voice_backend.BackendStartStatus.FAILED_CLOSED,
        voice_backend.BackendStartStatus.OWNER_BUSY,
        voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
    ],
)
def test_unadmitted_typed_input_result_rolls_back_real_ingress_and_retries(
    status: voice_backend.BackendStartStatus,
) -> None:
    """Every attempted input result rolls back before output abort and can retry."""
    backend = _ScriptedWakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    initial_stop = ingress.stop_for_sleep()
    assert initial_stop is not None
    assert initial_stop.definitively_closed
    backend.next_start_status = status
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress = ingress
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)
    media.resume_after_wake.return_value = voice_media.MediaPowerTransitionResult(
        "resumed",
        101,
        "fresh_output",
    )
    media.admit_wake_start.return_value = True
    abort_saw_closed_input = threading.Event()

    def _abort_after_input_close(**_kwargs: object) -> voice_media.MediaPowerTransitionResult:
        assert (
            backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED
        )
        abort_saw_closed_input.set()
        return voice_media.MediaPowerTransitionResult(
            "suspended",
            101,
            "output_aborted_after_input_rollback",
        )

    media.abort_wake_start.side_effect = _abort_after_input_close
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    failed = coordinator.on_wake()
    assert isinstance(failed.input_result, voice_backend.BackendStartResult)
    assert failed.input_result.status is status
    assert abort_saw_closed_input.is_set()
    assert ingress._control_intent == "suspended"
    assert ingress._suspended
    assert (
        backend.ownership_snapshot().state
        is voice_backend.BackendLifecycleState.CLOSED
    )
    assert backend.active_owner_count == 0
    media.admit_wake_start.assert_not_called()

    retry = coordinator.on_wake()
    assert isinstance(retry.input_result, voice_backend.BackendStartResult)
    assert retry.input_result.started
    assert retry.input_skipped_reason is None
    assert backend.active_owner_count == 1
    assert backend.max_active_owner_count == 1
    media.admit_wake_start.assert_called_once_with(attempt_id=101)
    assert coordinator.close(timeout_s=0.2)
    assert ingress.close().definitively_closed


def test_started_input_is_exactly_closed_when_output_admit_cas_loses() -> None:
    """A successful input reopen is rolled back if output admission loses CAS."""
    backend = _FakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    initial_stop = ingress.stop_for_sleep()
    assert initial_stop is not None
    assert initial_stop.definitively_closed
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress = ingress
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)
    media.resume_after_wake.return_value = voice_media.MediaPowerTransitionResult(
        "resumed",
        111,
        "fresh_output",
    )
    media.admit_wake_start.side_effect = [False, True]

    def _abort_after_input_close(**_kwargs: object) -> voice_media.MediaPowerTransitionResult:
        assert (
            backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED
        )
        return voice_media.MediaPowerTransitionResult(
            "suspended",
            111,
            "output_admit_cas_lost",
        )

    media.abort_wake_start.side_effect = _abort_after_input_close
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    lost = coordinator.on_wake()
    assert isinstance(lost.input_result, voice_backend.BackendStartResult)
    assert lost.input_result.started
    assert lost.input_skipped_reason is not None
    assert ingress._control_intent == "suspended"
    assert ingress._suspended
    assert backend.active_owner_count == 0
    assert backend.stop_count == 2

    retry = coordinator.on_wake()
    assert isinstance(retry.input_result, voice_backend.BackendStartResult)
    assert retry.input_result.started
    assert retry.input_skipped_reason is None
    assert backend.active_owner_count == 1
    assert backend.max_active_owner_count == 1
    assert coordinator.close(timeout_s=0.2)
    assert ingress.close().definitively_closed


def test_pending_wake_input_failure_rolls_back_real_ingress_before_output_abort() -> None:
    """The pending continuation uses the same input-first transaction rollback."""
    backend = _ScriptedWakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    initial_stop = ingress.stop_for_sleep()
    assert initial_stop is not None
    assert initial_stop.definitively_closed
    backend.next_start_status = voice_backend.BackendStartStatus.FAILED_CLOSED
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress = ingress
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)
    media.resume_after_wake.side_effect = [
        voice_media.MediaPowerTransitionResult(
            "uncertain",
            121,
            "prior_stop_debt",
            helper_thread_alive=True,
        ),
        voice_media.MediaPowerTransitionResult("resumed", 122, "debt_closed"),
        voice_media.MediaPowerTransitionResult("resumed", 123, "retry_output"),
    ]
    media.admit_wake_start.return_value = True
    rollback_before_abort = threading.Event()

    def _abort_after_input_close(**_kwargs: object) -> voice_media.MediaPowerTransitionResult:
        assert ingress._control_intent == "suspended"
        assert (
            backend.ownership_snapshot().state
            is voice_backend.BackendLifecycleState.CLOSED
        )
        rollback_before_abort.set()
        return voice_media.MediaPowerTransitionResult(
            "suspended",
            122,
            "pending_output_aborted",
        )

    media.abort_wake_start.side_effect = _abort_after_input_close
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    initial = coordinator.on_wake()
    assert initial.output_result is not None
    assert initial.output_result.helper_thread_alive
    assert rollback_before_abort.wait(timeout=1.0)
    pending = coordinator._pending_wake_thread
    assert pending is not None
    pending.join(timeout=1.0)
    assert not pending.is_alive()
    assert ingress._control_intent == "suspended"
    assert backend.active_owner_count == 0

    retry = coordinator.on_wake()
    assert isinstance(retry.input_result, voice_backend.BackendStartResult)
    assert retry.input_result.started
    assert retry.input_skipped_reason is None
    assert backend.active_owner_count == 1
    assert backend.max_active_owner_count == 1
    assert coordinator.close(timeout_s=0.2)
    assert ingress.close().definitively_closed


def test_typed_input_start_debt_re_suspends_real_media_owner(tmp_path: Path) -> None:
    """A typed input failure closes the fresh output and restores actor suspension."""

    class _TypedInputGatePlayer:
        def __init__(self) -> None:
            self.active = False
            self.start_calls = 0
            self.stop_calls = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.active = True
            return voice_tts.PlayerStartResult("started", 91, "fresh_output")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            self.active = False
            return voice_tts.PlayerStopResult("closed", 92, "output_closed")

    db_path = tmp_path / "typed-input-debt-media-gate.db"
    open_event_log(db_path).close()
    player = _TypedInputGatePlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.resume_after_wake.return_value = voice_backend.BackendStartResult(
        status=voice_backend.BackendStartStatus.OPEN_UNCERTAIN,
        stream_epoch=4,
        profile=None,
        reason="input_open_debt",
        attempt_id="input-a4",
    )
    coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
    transition = coordinator.on_wake()
    assert transition.output_result is not None
    assert transition.output_result.status == "resumed"
    assert transition.input_skipped_reason is not None
    assert "input_resume_open_uncertain" in transition.input_skipped_reason
    assert player.start_calls == 1
    assert player.stop_calls == 2
    assert not player.active
    assert media._power_state == "suspended"
    assert media._power_suspended
    assert not media._accepting.is_set()
    assert coordinator.close(timeout_s=0.2)
    assert media.close(wait_timeout_s=0.5)


def test_power_coordinator_real_media_debt_keeps_real_ingress_suspended(
    tmp_path: Path,
) -> None:
    """A failed physical output close blocks wake input under one total bound."""

    class _OutputStream:
        active = False

        def __init__(self) -> None:
            self.close_calls = 0

        def start(self) -> None:
            self.active = True

        def stop(self) -> None:
            self.active = False

        def close(self) -> None:
            self.close_calls += 1
            if self.close_calls == 1:
                message = "injected output close debt"
                raise OSError(message)

    db_path = tmp_path / "power-output-debt.db"
    open_event_log(db_path).close()
    backend = _FakeBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress = ingress
    stream = _OutputStream()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    with patch.object(voice_tts, "_open_output_stream", return_value=stream) as opened:
        player = voice_tts.AudioStreamPlayer(lazy_open=True, generation_safe=True)
        media = voice_media.StreamingTTSPipeline(
            provider=provider,
            player=player,
            conn_factory=lambda: open_event_log(db_path),
            boot_high_water_id=0,
            config=replace(
                voice_media.StreamingMediaConfig(),
                shutdown_timeout_s=0.5,
            ),
        )
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        slept = coordinator.before_sleep()
        assert slept.output_result is not None
        assert slept.output_result.status == "uncertain"
        assert slept.elapsed_s < coordinator._TOTAL_TRANSITION_BOUND_S
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.status == "uncertain"
        assert woke.input_result is None
        assert woke.input_skipped_reason is not None
        assert woke.elapsed_s < coordinator._TOTAL_TRANSITION_BOUND_S
        assert backend.start_count == 1
        assert ingress.capability.state is voice_audio.InputCapabilityState.OUTPUT_UNAVAILABLE
        assert opened.call_count == 1
        assert media.close(wait_timeout_s=1.0)
    assert ingress.close().definitively_closed


def test_power_coordinator_real_media_success_preserves_cross_device_order(
    tmp_path: Path,
) -> None:
    """Fresh output starts before the real ingress opens its wake epoch."""
    actions: list[str] = []

    class _OrderedBackend(_FakeBackend):
        def start(
            self,
            *,
            stream_epoch: int,
            attempt_id: str,
            frame_sink: voice_backend.InputFrameSink,
            render_source: voice_backend.RenderSource | None = None,
            timeout_s: float | None = None,
        ) -> voice_backend.BackendStartResult:
            actions.append("input_start")
            return super().start(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                frame_sink=frame_sink,
                render_source=render_source,
                timeout_s=timeout_s,
            )

        def stop(
            self,
            *,
            stream_epoch: int,
            attempt_id: str,
            timeout_s: float | None = None,
        ) -> voice_backend.BackendStopResult:
            actions.append("input_stop")
            return super().stop(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                timeout_s=timeout_s,
            )

    class _OrderedOutputStream:
        active = False

        def start(self) -> None:
            actions.append("output_start")
            self.active = True

        def stop(self) -> None:
            actions.append("output_stop")
            self.active = False

        def close(self) -> None:
            actions.append("output_close")

    db_path = tmp_path / "power-output-success.db"
    open_event_log(db_path).close()
    backend = _OrderedBackend()
    ingress = _ingress(backend)
    assert ingress.start().started
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress = ingress
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    with patch.object(
        voice_tts,
        "_open_output_stream",
        side_effect=lambda **_kwargs: _OrderedOutputStream(),
    ):
        media = voice_media.StreamingTTSPipeline(
            provider=provider,
            player=voice_tts.AudioStreamPlayer(lazy_open=True, generation_safe=True),
            conn_factory=lambda: open_event_log(db_path),
            boot_high_water_id=0,
            config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.5),
        )
        actions.clear()
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        slept = coordinator.before_sleep()
        woke = coordinator.on_wake()
        assert slept.output_result is not None
        assert slept.output_result.succeeded
        assert woke.output_result is not None
        assert woke.output_result.status == "resumed"
        assert actions[:5] == [
            "input_stop",
            "output_stop",
            "output_close",
            "output_start",
            "input_start",
        ]
        assert slept.elapsed_s < coordinator._TOTAL_TRANSITION_BOUND_S
        assert woke.elapsed_s < coordinator._TOTAL_TRANSITION_BOUND_S
        assert media.close(wait_timeout_s=1.0)
    assert ingress.close().definitively_closed


def test_pending_wake_auto_resumes_output_then_input_after_late_sleep_stop(
    tmp_path: Path,
) -> None:
    """One consumed OS wake is replayed once after the exact stop debt closes."""

    class _PendingWakePlayer:
        def __init__(self) -> None:
            self.stop_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.active_owners = 0
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.active_owners += 1
            self.max_owners = max(self.max_owners, self.active_owners)
            return voice_tts.PlayerStartResult("started", self.start_calls, "opened")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            if self.stop_calls == 1:
                assert self.stop_release.wait(timeout=1.0)
            self.active_owners = 0
            return voice_tts.PlayerStopResult("closed", self.stop_calls, "closed")

    db_path = tmp_path / "pending-wake-replay.db"
    open_event_log(db_path).close()
    player = _PendingWakePlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    input_resumed = threading.Event()
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None

    def _resume_input(**_kwargs: object) -> object:
        input_resumed.set()
        return object()

    session.ingress.resume_after_wake.side_effect = _resume_input
    with patch.object(
        inherent_loop._VoicePowerCoordinator,
        "_TOTAL_TRANSITION_BOUND_S",
        0.05,
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        slept = coordinator.before_sleep()
        assert slept.output_result is not None
        assert slept.output_result.status == "uncertain"
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.status == "uncertain"
        assert woke.output_result.helper_thread_alive
        _wait_until(
            lambda: coordinator._pending_wake_thread is not None
            and coordinator._pending_wake_thread.is_alive(),
        )
        duplicate_results: list[inherent_loop._VoicePowerTransition] = []
        duplicate = threading.Thread(
            target=lambda: duplicate_results.append(coordinator.on_wake()),
        )
        duplicate.start()
        player.stop_release.set()
        assert input_resumed.wait(timeout=1.0)
        duplicate.join(timeout=1.0)
        assert not duplicate.is_alive()
        assert len(duplicate_results) == 1
        assert player.start_calls == 1
        assert player.max_owners == 1
        session.ingress.resume_after_wake.assert_called_once()
        assert coordinator.close(timeout_s=0.2)
    assert media.close(wait_timeout_s=0.5)


def test_pending_wake_retries_one_exact_debt_beyond_two_transition_bounds(
    tmp_path: Path,
) -> None:
    """A consumed wake remains recoverable after multiple bounded joins."""

    class _VeryLateStopPlayer:
        def __init__(self) -> None:
            self.stop_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.active_owners = 0
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.active_owners += 1
            self.max_owners = max(self.max_owners, self.active_owners)
            return voice_tts.PlayerStartResult("started", self.start_calls, "opened")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            if self.stop_calls == 1:
                assert self.stop_release.wait(timeout=1.0)
            self.active_owners = 0
            return voice_tts.PlayerStopResult("closed", self.stop_calls, "closed")

    db_path = tmp_path / "pending-wake-multiple-bounds.db"
    open_event_log(db_path).close()
    player = _VeryLateStopPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    input_resumed = threading.Event()
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None

    def _resume_very_late_input(**_kwargs: object) -> object:
        input_resumed.set()
        return object()

    session.ingress.resume_after_wake.side_effect = _resume_very_late_input
    with patch.object(
        inherent_loop._VoicePowerCoordinator,
        "_TOTAL_TRANSITION_BOUND_S",
        0.02,
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        assert coordinator.before_sleep().output_result is not None
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.helper_thread_alive
        _wait_until(lambda: coordinator._pending_wake_state == "in_flight")
        time.sleep(0.08)
        assert coordinator._pending_wake_thread is not None
        assert coordinator._pending_wake_thread.is_alive()
        assert coordinator._pending_wake_state == "in_flight"
        assert player.start_calls == 0
        player.stop_release.set()
        assert input_resumed.wait(timeout=1.0)
        _wait_until(lambda: coordinator._pending_wake_state == "recovered")
        assert player.start_calls == 1
        assert player.max_owners == 1
        session.ingress.resume_after_wake.assert_called_once()
        assert coordinator.close(timeout_s=0.2)
    assert media.close(wait_timeout_s=0.5)


def test_new_sleep_revokes_pending_wake_before_operation_lock_wait() -> None:
    """Deadline-consuming continuation cannot starve or outlive a newer sleep."""
    debt_release = threading.Event()
    resume_calls = 0
    media = MagicMock(spec=voice_media.StreamingTTSPipeline)
    media.suspend_for_sleep.return_value = voice_media.MediaPowerTransitionResult(
        "suspended",
        1,
        "closed",
    )

    def _resume_output(*, deadline: float) -> voice_media.MediaPowerTransitionResult:
        nonlocal resume_calls
        resume_calls += 1
        while not debt_release.is_set() and time.monotonic() < deadline:
            time.sleep(0.001)
        if debt_release.is_set():
            return voice_media.MediaPowerTransitionResult("resumed", 50, "late_closed")
        return voice_media.MediaPowerTransitionResult(
            "uncertain",
            50,
            "exact_debt_still_alive",
            helper_thread_alive=True,
            deadline_exhausted=True,
        )

    media.resume_after_wake.side_effect = _resume_output
    media.abort_wake_start.return_value = voice_media.MediaPowerTransitionResult(
        "suspended",
        50,
        "stale_wake_aborted",
    )
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None
    with patch.object(
        inherent_loop._VoicePowerCoordinator,
        "_TOTAL_TRANSITION_BOUND_S",
        0.025,
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        coordinator.before_sleep()
        first_wake = coordinator.on_wake()
        assert first_wake.output_result is not None
        assert first_wake.output_result.helper_thread_alive
        _wait_until(lambda: resume_calls >= 2)
        session.ingress.report_output_unavailable.reset_mock()
        second_sleep = coordinator.before_sleep()
        assert second_sleep.output_result is not None
        assert second_sleep.output_result.status == "suspended"
        assert second_sleep.elapsed_s < coordinator._TOTAL_TRANSITION_BOUND_S
        debt_release.set()
        assert coordinator._pending_wake_thread is not None
        coordinator._pending_wake_thread.join(timeout=1.0)
        assert not coordinator._pending_wake_thread.is_alive()
        session.ingress.resume_after_wake.assert_not_called()
        session.ingress.report_output_unavailable.assert_not_called()
        media.admit_wake_start.assert_not_called()
        media.abort_wake_start.assert_called()
        assert coordinator.close(timeout_s=0.2)


def test_late_actor_resume_future_rejoins_and_resumes_input_exactly_once(
    tmp_path: Path,
) -> None:
    """Actor admission timeout remains an exact joinable wake attempt."""

    class _ActorResumePlayer:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0
            self.active_owners = 0
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.active_owners += 1
            self.max_owners = max(self.max_owners, self.active_owners)
            return voice_tts.PlayerStartResult("started", self.start_calls, "opened")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            self.active_owners = 0
            return voice_tts.PlayerStopResult("closed", self.stop_calls, "closed")

    db_path = tmp_path / "pending-wake-actor-future.db"
    open_event_log(db_path).close()
    player = _ActorResumePlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    actor_resume_release = threading.Event()

    async def _late_actor_resume(**_kwargs: object) -> bool:
        await asyncio.to_thread(actor_resume_release.wait)
        return True

    input_resumed = threading.Event()
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None

    def _resume_after_actor(**_kwargs: object) -> object:
        input_resumed.set()
        return object()

    session.ingress.resume_after_wake.side_effect = _resume_after_actor
    with (
        patch.object(
            inherent_loop._VoicePowerCoordinator,
            "_TOTAL_TRANSITION_BOUND_S",
            0.025,
        ),
        patch.object(media, "_resume_after_wake_owned", new=_late_actor_resume),
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        assert coordinator.before_sleep().output_result is not None
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.reason == "actor_resume_timeout"
        assert woke.output_result.helper_thread_alive
        _wait_until(lambda: coordinator._pending_wake_state == "in_flight")
        time.sleep(0.06)
        assert player.start_calls == 1
        assert not input_resumed.is_set()
        actor_resume_release.set()
        assert input_resumed.wait(timeout=1.0)
        _wait_until(lambda: coordinator._pending_wake_state == "recovered")
        assert player.start_calls == 1
        assert player.max_owners == 1
        session.ingress.resume_after_wake.assert_called_once()
        assert coordinator.close(timeout_s=0.2)
    assert media.close(wait_timeout_s=0.5)


def test_stale_actor_resume_future_cannot_undo_newer_sleep(
    tmp_path: Path,
) -> None:
    """Actor admission side effects CAS against the exact power generation."""
    db_path = tmp_path / "stale-actor-resume-generation.db"
    open_event_log(db_path).close()
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.start.return_value = voice_tts.PlayerStartResult("started", 71, "opened")
    player.stop.return_value = voice_tts.PlayerStopResult("closed", 72, "closed")
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    old_future_release = threading.Event()
    original_resume = media._resume_after_wake_owned

    async def _delayed_resume(**kwargs: object) -> bool:
        await asyncio.to_thread(old_future_release.wait)
        attempt_id = kwargs["attempt_id"]
        admission_generation = kwargs["admission_generation"]
        assert isinstance(attempt_id, int)
        assert isinstance(admission_generation, int)
        return await original_resume(
            attempt_id=attempt_id,
            admission_generation=admission_generation,
        )

    with patch.object(media, "_resume_after_wake_owned", new=_delayed_resume):
        stale_wake = media.resume_after_wake(timeout_s=0.02)
        assert stale_wake.status == "uncertain"
        assert stale_wake.reason == "actor_resume_timeout"
        newer_sleep = media.suspend_for_sleep(timeout_s=0.2)
        assert newer_sleep.status == "suspended"
        assert media._power_suspended
        assert not media._accepting.is_set()
        old_future_release.set()
        future = media._power_actor_resume_future
        assert future is not None
        assert future.result(timeout=1.0) is False
        assert media._power_state == "suspended"
        assert media._power_suspended
        assert not media._accepting.is_set()
    assert media.close(wait_timeout_s=0.5)


def test_new_sleep_revokes_wake_start_blocked_inside_foreign_open(
    tmp_path: Path,
) -> None:
    """A start returning after sleep is compensating-close debt, never admitted."""

    class _SleepRevokedStartPlayer:
        def __init__(self) -> None:
            self.start_entered = threading.Event()
            self.start_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.physical_open = False
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.start_entered.set()
            assert self.start_release.wait(timeout=1.0)
            self.physical_open = True
            self.max_owners = max(self.max_owners, int(self.physical_open))
            return voice_tts.PlayerStartResult("started", 76, "late_wake_open")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            self.physical_open = False
            return voice_tts.PlayerStopResult("closed", 77, "exact_wake_close")

    db_path = tmp_path / "sleep-revokes-blocked-wake-open.db"
    open_event_log(db_path).close()
    player = _SleepRevokedStartPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    wake_results: list[voice_media.MediaPowerTransitionResult] = []
    waking = threading.Thread(
        target=lambda: wake_results.append(media.resume_after_wake(timeout_s=0.5)),
    )
    waking.start()
    assert player.start_entered.wait(timeout=1.0)
    newer_sleep = media.suspend_for_sleep(timeout_s=0.02)
    assert newer_sleep.status == "uncertain"
    assert newer_sleep.helper_thread_alive
    player.start_release.set()
    waking.join(timeout=1.0)
    assert not waking.is_alive()
    assert wake_results[-1].status == "uncertain"
    _wait_until(lambda: media._power_state == "suspended")
    assert player.start_calls == 1
    assert player.stop_calls == 2
    assert player.max_owners == 1
    assert not player.physical_open
    assert media._power_suspended
    assert not media._accepting.is_set()
    assert media.close(wait_timeout_s=0.5)


def test_pending_wake_is_revoked_by_shutdown_without_output_reopen(
    tmp_path: Path,
) -> None:
    """Shutdown consumes pending wake generation before any fresh output open."""

    class _ShutdownPendingPlayer:
        def __init__(self) -> None:
            self.stop_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            return voice_tts.PlayerStartResult("started", self.start_calls, "opened")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            if self.stop_calls == 1:
                assert self.stop_release.wait(timeout=1.0)
            return voice_tts.PlayerStopResult("closed", self.stop_calls, "closed")

    db_path = tmp_path / "pending-wake-shutdown.db"
    open_event_log(db_path).close()
    player = _ShutdownPendingPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None
    with patch.object(
        inherent_loop._VoicePowerCoordinator,
        "_TOTAL_TRANSITION_BOUND_S",
        0.04,
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        coordinator.before_sleep()
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.helper_thread_alive
        _wait_until(
            lambda: coordinator._pending_wake_thread is not None
            and coordinator._pending_wake_thread.is_alive(),
        )
        coordinator.close(timeout_s=0.01)
        media.request_close()
        player.stop_release.set()
        assert media.close(wait_timeout_s=0.5)
        assert coordinator.close(timeout_s=0.2)
    assert player.start_calls == 0
    session.ingress.resume_after_wake.assert_not_called()


def test_media_sleep_timeout_late_terminalization_continues_exact_stop_and_wake(
    tmp_path: Path,
) -> None:
    """Late actor success completes its exact stop before a fresh wake start."""
    db_path = tmp_path / "late-sleep-terminalization.db"
    open_event_log(db_path).close()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.stop.return_value = voice_tts.PlayerStopResult(
        "closed",
        31,
        "close_returned",
    )
    player.start.return_value = voice_tts.PlayerStartResult(
        "started",
        32,
        "stream_started",
    )
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.2),
        start_player=False,
    )
    release_terminalization = threading.Event()

    async def _late_terminalization() -> bool:
        await asyncio.to_thread(release_terminalization.wait)
        return True

    with patch.object(
        media,
        "_suspend_for_sleep_owned",
        new=_late_terminalization,
    ):
        timed_out = media.suspend_for_sleep(timeout_s=0.02)
        assert timed_out.status == "uncertain"
        assert timed_out.deadline_exhausted
        assert timed_out.helper_thread_alive
        attempt_id = timed_out.attempt_id
        assert player.stop.call_count == 0
        release_terminalization.set()
        _wait_until(lambda: media._power_state == "suspended")
        assert media._power_attempt_id == attempt_id
        assert player.stop.call_count == 1
        assert media._power_helper is not None
        media._power_helper.join(timeout=1.0)
        assert not media._power_helper.is_alive()
        woke = media.resume_after_wake(timeout_s=0.5)
        assert woke.status == "resumed"
        assert player.start.call_count == 1
        assert media._power_attempt_id == attempt_id + 1
    assert media.close(wait_timeout_s=1.0)


def test_media_shutdown_revokes_blocked_wake_start_and_late_closes_exact_owner(
    tmp_path: Path,
) -> None:
    """A start returning after actor exit compensates and completes cleanup."""

    class _BlockedWakePlayer:
        def __init__(self) -> None:
            self.start_entered = threading.Event()
            self.start_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.physical_open = False
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.start_entered.set()
            assert self.start_release.wait(timeout=1.0)
            self.physical_open = True
            self.max_owners = max(self.max_owners, int(self.physical_open))
            return voice_tts.PlayerStartResult(
                "started",
                self.start_calls,
                "late_stream_started",
            )

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            self.physical_open = False
            return voice_tts.PlayerStopResult(
                "closed",
                self.stop_calls,
                "exact_stream_closed",
            )

    db_path = tmp_path / "shutdown-revokes-wake-start.db"
    open_event_log(db_path).close()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    player = _BlockedWakePlayer()
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.04),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    wake = media.resume_after_wake(timeout_s=0.03)
    assert wake.status == "uncertain"
    assert wake.helper_thread_alive
    assert player.start_entered.wait(timeout=1.0)
    assert not media.close(wait_timeout_s=0.04)
    _wait_until(media._closed.is_set)
    assert not media.cleanup_complete
    player.start_release.set()
    _wait_until(lambda: media.cleanup_complete)
    assert not player.physical_open
    assert player.start_calls == 1
    assert player.stop_calls == 2  # initial suspend + late-start compensation
    assert player.max_owners == 1
    assert media._power_state == "suspended"
    assert media.close(wait_timeout_s=0.2)


def test_composition_shutdown_revokes_blocked_wake_start_before_input_close(
    tmp_path: Path,
) -> None:
    """Coordinator revoke closes a late wake owner while input shutdown blocks."""

    class _ShutdownBarrierPlayer:
        def __init__(self) -> None:
            self.start_entered = threading.Event()
            self.start_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.physical_open = False
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.start_entered.set()
            assert self.start_release.wait(timeout=1.0)
            self.physical_open = True
            self.max_owners = max(self.max_owners, int(self.physical_open))
            return voice_tts.PlayerStartResult("started", self.start_calls, "late_open")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            self.physical_open = False
            return voice_tts.PlayerStopResult("closed", self.stop_calls, "closed")

    db_path = tmp_path / "composition-shutdown-revoke.db"
    open_event_log(db_path).close()
    player = _ShutdownBarrierPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.05),
        start_player=False,
    )
    input_close_entered = threading.Event()
    input_close_release = threading.Event()
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    session.ingress.stop_for_sleep.return_value = None

    def _close_input() -> voice_session.VoiceSessionCloseResult:
        input_close_entered.set()
        assert input_close_release.wait(timeout=1.0)
        return voice_session.VoiceSessionCloseResult(
            ingress=voice_audio.IngressCloseResult(
                definitively_closed=True,
                stream_epoch=1,
                backend_result=None,
                worker_alive=False,
                open_subscribers=0,
            ),
            alive_threads=(),
            pending_detections=0,
            pending_commits=0,
        )

    session.close.side_effect = _close_input
    owners = inherent_loop._VoiceInputOwners(
        duplex_session=session,
        wake_listener=None,
        wake_stream=None,
        single_ingress_attempted=True,
    )
    with patch.object(
        inherent_loop._VoicePowerCoordinator,
        "_TOTAL_TRANSITION_BOUND_S",
        0.025,
    ):
        coordinator = inherent_loop._VoicePowerCoordinator(session=session, media=media)
        coordinator.before_sleep()
        woke = coordinator.on_wake()
        assert woke.output_result is not None
        assert woke.output_result.helper_thread_alive
        assert player.start_entered.wait(timeout=1.0)
        coordinator.close(timeout_s=0.01)
        shutdown = threading.Thread(
            target=lambda: inherent_loop._request_voice_input_branch_shutdown(
                owners,
                media,
            ),
        )
        shutdown.start()
        assert input_close_entered.wait(timeout=1.0)
        assert not media._shutdown_requested.is_set()
        player.start_release.set()
        _wait_until(lambda: player.stop_calls >= 2 and not player.physical_open)
        assert input_close_entered.is_set()
        assert player.start_calls == 1
        assert player.max_owners == 1
        input_close_release.set()
        shutdown.join(timeout=1.0)
        assert not shutdown.is_alive()
    assert media.close(wait_timeout_s=0.5)


def test_late_compensating_stop_debt_auto_converges_cleanup_ledger(
    tmp_path: Path,
) -> None:
    """One janitor joins late close debt without a second external close call."""

    class _LateCompensationPlayer:
        def __init__(self) -> None:
            self.start_entered = threading.Event()
            self.start_release = threading.Event()
            self.compensation_seen = threading.Event()
            self.debt_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.physical_open: bool = False
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.start_entered.set()
            assert self.start_release.wait(timeout=1.0)
            self.physical_open = True
            self.max_owners = max(self.max_owners, int(self.physical_open))
            return voice_tts.PlayerStartResult("started", 41, "late_open")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            if self.stop_calls == 1:
                return voice_tts.PlayerStopResult("closed", 40, "initial_suspend")
            self.compensation_seen.set()
            if not self.debt_release.is_set():
                return voice_tts.PlayerStopResult(
                    "uncertain",
                    42,
                    "exact_close_helper_in_flight",
                    helper_thread_alive=True,
                )
            self.physical_open = False
            return voice_tts.PlayerStopResult("closed", 42, "late_exact_close")

    db_path = tmp_path / "late-compensation-janitor.db"
    open_event_log(db_path).close()
    player = _LateCompensationPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.03),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    wake = media.resume_after_wake(timeout_s=0.02)
    assert wake.status == "uncertain"
    assert player.start_entered.wait(timeout=1.0)
    assert not media.close(wait_timeout_s=0.03)
    _wait_until(media._closed.is_set)
    player.start_release.set()
    assert player.compensation_seen.wait(timeout=1.0)
    assert not media.cleanup_complete
    _wait_until(lambda: player.physical_open)
    player.debt_release.set()
    _wait_until(lambda: media.cleanup_complete)
    _wait_until(lambda: not player.physical_open)
    assert player.start_calls == 1
    assert player.max_owners == 1
    assert media._player_stop_thread is not None
    media._player_stop_thread.join(timeout=1.0)
    assert not media._player_stop_thread.is_alive()


def test_open_unadmitted_shutdown_stop_debt_auto_converges_cleanup_ledger(
    tmp_path: Path,
) -> None:
    """Shutdown joins an already-open wake owner until exact close completes."""

    class _OpenUnadmittedDebtPlayer:
        def __init__(self) -> None:
            self.compensation_seen = threading.Event()
            self.debt_release = threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            self.physical_open = False
            self.max_owners = 0

        def start(self) -> voice_tts.PlayerStartResult:
            self.start_calls += 1
            self.physical_open = True
            self.max_owners = max(self.max_owners, int(self.physical_open))
            return voice_tts.PlayerStartResult("started", 52, "wake_owner_open")

        def stop(self, *, timeout_s: float | None = None) -> voice_tts.PlayerStopResult:
            del timeout_s
            self.stop_calls += 1
            if self.stop_calls == 1:
                return voice_tts.PlayerStopResult("closed", 51, "initial_suspend")
            self.compensation_seen.set()
            if not self.debt_release.is_set():
                return voice_tts.PlayerStopResult(
                    "uncertain",
                    53,
                    "open_unadmitted_close_debt",
                    helper_thread_alive=True,
                )
            self.physical_open = False
            return voice_tts.PlayerStopResult("closed", 53, "wake_owner_closed")

    db_path = tmp_path / "open-unadmitted-shutdown-debt.db"
    open_event_log(db_path).close()
    player = _OpenUnadmittedDebtPlayer()
    provider = MagicMock()
    provider.streaming_candidate_count = 1
    media = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,  # type: ignore[arg-type]
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=replace(voice_media.StreamingMediaConfig(), shutdown_timeout_s=0.03),
        start_player=False,
    )
    assert media.suspend_for_sleep(timeout_s=0.2).status == "suspended"
    opened = media._transition_player_for_power(
        action="start",
        deadline=time.monotonic() + 0.2,
    )
    assert opened.status == "resumed"
    assert media._power_state == "open_unadmitted"
    assert player.physical_open
    media.revoke_wake_starts_for_shutdown()
    assert player.compensation_seen.wait(timeout=1.0)
    assert not media.close(wait_timeout_s=0.03)
    _wait_until(media._closed.is_set)
    assert not media.cleanup_complete
    assert player.start_calls == 1
    assert player.max_owners == 1
    player.debt_release.set()
    _wait_until(lambda: not player.physical_open)
    _wait_until(lambda: media.cleanup_complete)
    assert media._power_state == "suspended"
    assert media._power_helper is not None
    media._power_helper.join(timeout=1.0)
    assert not media._power_helper.is_alive()


def test_wave3_prewarm_is_flag_gated_and_failure_preserves_legacy_before_device() -> None:
    """SenseVoice prewarm never changes feature-off or claims a mic on failure."""
    runtime = MagicMock()
    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    runtime.config = {"realtime": {"enabled": True}}
    pipeline = MagicMock(spec=voice_pipeline.VoicePipeline)
    broadcaster = MagicMock()
    wave2 = object.__new__(voice_media.StreamingTTSPipeline)
    disabled = inherent_loop._spawn_single_ingress_session(
        runtime=runtime,
        pipeline=pipeline,
        broadcaster=broadcaster,
        silero_path=MagicMock(),
        tts=wave2,
    )
    assert disabled == (None, False)
    pipeline.prewarm_input_model.assert_not_called()

    runtime.config = {
        "realtime": {
            "enabled": True,
            "single_audio_ingress": {"enabled": True},
            "streaming_output": {"enabled": True},
        },
    }
    pipeline.prewarm_input_model.side_effect = RuntimeError("injected prewarm")
    engine = MagicMock()
    with (
        patch.object(voice_wake, "WakeEngine", return_value=engine),
        patch.object(
            voice_backend,
            "SoundDeviceDuplexBackend",
            side_effect=AssertionError("device must not be constructed"),
        ),
    ):
        failed = inherent_loop._spawn_single_ingress_session(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=wave2,
        )
    assert failed == (None, False)
    pipeline.prewarm_input_model.assert_called_once_with()
    engine.start.assert_called_once_with()
    engine.close.assert_called_once_with()


def test_session_silero_prepare_failure_is_pre_device_and_enables_legacy() -> None:
    """Session preparation has a typed no-owner boundary before ingress.start."""
    runtime = MagicMock()
    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    runtime.config = {
        "realtime": {
            "enabled": True,
            "single_audio_ingress": {"enabled": True},
            "streaming_output": {"enabled": True},
        },
    }
    pipeline = MagicMock(spec=voice_pipeline.VoicePipeline)
    broadcaster = MagicMock()
    backend = _FakeBackend()
    engine = MagicMock()
    vad = MagicMock(spec=voice_audio.SileroVad)
    vad.prepare_utterance.side_effect = RuntimeError("injected Silero prepare failure")
    legacy_listener = MagicMock()
    legacy_stream = MagicMock()
    wave2 = object.__new__(voice_media.StreamingTTSPipeline)
    with (
        patch.object(voice_wake, "WakeEngine", return_value=engine),
        patch.object(voice_audio, "SileroVad", return_value=vad),
        patch.object(voice_backend, "SoundDeviceDuplexBackend", return_value=backend),
        patch.object(
            inherent_loop,
            "_spawn_wake_listener",
            return_value=(legacy_listener, legacy_stream),
        ) as legacy_spawn,
    ):
        owners = inherent_loop._spawn_voice_input_owners(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=wave2,
            ducker=MagicMock(),
        )
    assert backend.start_count == 0
    assert not owners.single_ingress_attempted
    assert owners.duplex_session is None
    assert owners.wake_listener is legacy_listener
    assert owners.wake_stream is legacy_stream
    legacy_spawn.assert_called_once()
    engine.close.assert_called_once_with()


@pytest.mark.parametrize("failure_site", ["silero", "session"])
def test_post_ingress_construction_failure_closes_capability_dispatcher(
    failure_site: str,
) -> None:
    """Partial Wave-3 construction cannot leak its observer lane or mic owner."""
    runtime = MagicMock()
    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    runtime.config = {
        "realtime": {
            "enabled": True,
            "single_audio_ingress": {"enabled": True},
            "streaming_output": {"enabled": True},
        },
    }
    pipeline = MagicMock(spec=voice_pipeline.VoicePipeline)
    broadcaster = MagicMock()
    backend = _FakeBackend()
    engine = MagicMock()
    wave2 = object.__new__(voice_media.StreamingTTSPipeline)
    before = sum(
        thread.name == "jarvis-audio-capability-dispatch" and thread.is_alive()
        for thread in threading.enumerate()
    )
    with (
        patch.object(voice_wake, "WakeEngine", return_value=engine),
        patch.object(voice_backend, "SoundDeviceDuplexBackend", return_value=backend),
    ):
        if failure_site == "silero":
            with patch.object(
                voice_audio,
                "SileroVad",
                side_effect=RuntimeError("injected Silero constructor failure"),
            ):
                result = inherent_loop._spawn_single_ingress_session(
                    runtime=runtime,
                    pipeline=pipeline,
                    broadcaster=broadcaster,
                    silero_path=MagicMock(),
                    tts=wave2,
                )
        else:
            with patch.object(
                voice_session,
                "DuplexVoiceSession",
                side_effect=RuntimeError("injected session constructor failure"),
            ):
                result = inherent_loop._spawn_single_ingress_session(
                    runtime=runtime,
                    pipeline=pipeline,
                    broadcaster=broadcaster,
                    silero_path=MagicMock(),
                    tts=wave2,
                )
    assert result == (None, False)
    assert backend.start_count == 0
    engine.close.assert_called_once_with()
    _wait_until(
        lambda: sum(
            thread.name == "jarvis-audio-capability-dispatch" and thread.is_alive()
            for thread in threading.enumerate()
        )
        == before,
    )


def test_legacy_listener_start_failure_is_local_and_ptt_remains_wired() -> None:
    """Legacy wake startup cleanup cannot clear an already-valid PTT pipeline."""
    runtime = MagicMock()
    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    runtime.config = {"realtime": {"enabled": True}}
    pipeline = MagicMock(spec=voice_pipeline.VoicePipeline)
    pipeline.run_turn.return_value = MagicMock()
    broadcaster = MagicMock()
    stream = MagicMock()
    engine = MagicMock()
    vad = MagicMock(spec=voice_audio.SileroVad)
    listener = MagicMock(spec=voice_wake.WakeListener)
    listener.start.side_effect = RuntimeError("injected listener thread start failure")
    listener.is_alive.return_value = False
    with (
        patch.object(inherent_loop, "_open_wake_input_stream", return_value=stream),
        patch.object(voice_wake, "WakeEngine", return_value=engine),
        patch.object(voice_audio, "SileroVad", return_value=vad),
        patch.object(voice_wake, "WakeListener", return_value=listener),
    ):
        owners = inherent_loop._spawn_voice_input_owners(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=None,
            ducker=MagicMock(),
        )
    ptt = inherent_loop._build_voice_pipeline_callable(pipeline)
    deps = InherentDeps(
        submit_callable=lambda _text: "T-submit",
        broadcaster=broadcaster,
        voice_pipeline_callable=ptt,
    )
    assert deps.voice_pipeline_callable is ptt
    ptt(b"RIFF", "T-ptt-after-wake-failure", "U-ptt", "S-ptt")
    pipeline.run_turn.assert_called_once()
    assert owners.wake_listener is None
    assert owners.wake_stream is None
    assert not owners.single_ingress_attempted
    listener.request_stop.assert_called_once_with()
    listener.join.assert_called_once()
    stream.stop.assert_called_once_with()
    stream.close.assert_called_once_with()
    engine.close.assert_called_once_with()


def test_composition_root_voice_input_matrix_retains_ptt_and_exact_shutdown_branch() -> None:
    """Serve's extracted branch selects one mic owner while PTT stays callable."""
    runtime = MagicMock()
    runtime.wave1_features = Wave1FeatureFlags(
        transactional_event_append=True,
        lifecycle_terminal_cas=True,
    )
    runtime.config = {"realtime": {"enabled": True}}
    pipeline = MagicMock(spec=voice_pipeline.VoicePipeline)
    pipeline.run_turn.return_value = MagicMock()
    ptt_callable = inherent_loop._build_voice_pipeline_callable(pipeline)
    ptt_callable(b"RIFF", "T-ptt", "U-ptt", "S-ptt")
    pipeline.run_turn.assert_called_once()
    broadcaster = MagicMock()
    ducker = MagicMock()
    wave2 = object.__new__(voice_media.StreamingTTSPipeline)
    legacy_listener = MagicMock()
    legacy_stream = MagicMock()

    with patch.object(
        inherent_loop,
        "_spawn_wake_listener",
        return_value=(legacy_listener, legacy_stream),
    ) as spawn_legacy:
        disabled = inherent_loop._spawn_voice_input_owners(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=wave2,
            ducker=ducker,
        )
    assert disabled.duplex_session is None
    assert disabled.wake_listener is legacy_listener
    assert not disabled.single_ingress_attempted
    spawn_legacy.assert_called_once()

    runtime.config = {
        "realtime": {
            "enabled": True,
            "single_audio_ingress": {"enabled": True},
            "streaming_output": {"enabled": True},
        },
    }
    pipeline.prewarm_input_model.side_effect = RuntimeError("injected prewarm")
    engine = MagicMock()
    with (
        patch.object(voice_wake, "WakeEngine", return_value=engine),
        patch.object(
            voice_backend,
            "SoundDeviceDuplexBackend",
            side_effect=AssertionError("device must not be constructed"),
        ),
        patch.object(
            inherent_loop,
            "_spawn_wake_listener",
            return_value=(legacy_listener, legacy_stream),
        ) as prewarm_legacy,
    ):
        prewarm_failed = inherent_loop._spawn_voice_input_owners(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=wave2,
            ducker=ducker,
        )
    assert prewarm_failed.wake_listener is legacy_listener
    assert not prewarm_failed.single_ingress_attempted
    prewarm_legacy.assert_called_once()

    with (
        patch.object(
            inherent_loop,
            "_spawn_single_ingress_session",
            return_value=(None, True),
        ),
        patch.object(
            inherent_loop,
            "_spawn_wake_listener",
            side_effect=AssertionError("attempted device must forbid a second mic"),
        ),
    ):
        attempted_uncertain = inherent_loop._spawn_voice_input_owners(
            runtime=runtime,
            pipeline=pipeline,
            broadcaster=broadcaster,
            silero_path=MagicMock(),
            tts=wave2,
            ducker=ducker,
        )
    assert attempted_uncertain.duplex_session is None
    assert attempted_uncertain.wake_listener is None
    assert attempted_uncertain.single_ingress_attempted

    actions: list[str] = []
    duplex = MagicMock(spec=voice_session.DuplexVoiceSession)
    wave3_owners = inherent_loop._VoiceInputOwners(
        duplex_session=duplex,
        wake_listener=None,
        wake_stream=None,
        single_ingress_attempted=True,
    )
    legacy_owners = inherent_loop._VoiceInputOwners(
        duplex_session=None,
        wake_listener=legacy_listener,
        wake_stream=legacy_stream,
        single_ingress_attempted=False,
    )
    with (
        patch.object(
            inherent_loop,
            "_shutdown_duplex_voice_session",
            side_effect=lambda _session: actions.append("input_stop"),
        ),
        patch.object(
            inherent_loop,
            "_request_tts_close",
            side_effect=lambda _tts: actions.append("output_stop"),
        ),
        patch.object(
            inherent_loop,
            "_shutdown_wake",
            side_effect=lambda _listener, _stream: actions.append("legacy_stop"),
        ),
    ):
        inherent_loop._request_voice_input_branch_shutdown(wave3_owners, wave2)
        assert actions == ["input_stop", "output_stop"]
        actions.clear()
        inherent_loop._request_voice_input_branch_shutdown(legacy_owners, wave2)
        assert actions == ["output_stop", "legacy_stop"]


def test_input_capability_snapshot_is_versioned_ephemeral_and_reconnectable() -> None:
    """Reconnect gets latest L5 capability without writing an Event Log tick."""

    async def _scenario() -> None:
        broadcaster = InherentBroadcaster()
        await broadcaster.broadcast_voice_capability(
            version=2,
            state="available",
            stream_epoch=7,
            reason="input_stream_started",
            wake_available=True,
            local_capture_available=True,
            ptt_upload_available=True,
            text_available=True,
        )
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await broadcaster.register(ws)
        ws.send_json.assert_awaited_once()
        payload = ws.send_json.await_args.args[0]
        assert payload["op"] == "voice_capability"
        assert payload["payload"]["version"] == 2
        ws.send_json.reset_mock()
        await broadcaster.broadcast_voice_capability(
            version=1,
            state="stopped",
            stream_epoch=None,
            reason="stale",
            wake_available=False,
            local_capture_available=False,
            ptt_upload_available=True,
            text_available=True,
        )
        ws.send_json.assert_not_awaited()
        await broadcaster.broadcast_voice_capability(
            version=3,
            state="close_uncertain",
            stream_epoch=7,
            reason="device_close_timeout",
            wake_available=False,
            local_capture_available=False,
            ptt_upload_available=True,
            text_available=True,
        )
        ws.send_json.assert_awaited_once()

    asyncio.run(_scenario())


def test_broadcaster_registration_and_live_envelopes_share_one_sender_sequence() -> None:
    """Capability versions never regress and send_json calls never overlap."""

    async def _scenario() -> None:
        broadcaster = InherentBroadcaster()
        await broadcaster.broadcast_voice_capability(
            version=2,
            state="available",
            stream_epoch=2,
            reason="v2",
            wake_available=True,
            local_capture_available=True,
            ptt_upload_available=True,
            text_available=True,
        )
        messages: list[dict[str, object]] = []
        first_send_entered = asyncio.Event()
        release_first_send = asyncio.Event()
        active_sends = 0
        max_active_sends = 0

        async def _send_json(message: dict[str, object]) -> None:
            nonlocal active_sends, max_active_sends
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            try:
                messages.append(message)
                if len(messages) == 1:
                    first_send_entered.set()
                    await release_first_send.wait()
                else:
                    await asyncio.sleep(0)
            finally:
                active_sends -= 1

        ws = MagicMock()
        ws.send_json = _send_json
        registering = asyncio.create_task(broadcaster.register(ws))
        await asyncio.wait_for(first_send_entered.wait(), timeout=1.0)
        newer = asyncio.create_task(
            broadcaster.broadcast_voice_capability(
                version=3,
                state="suspended",
                stream_epoch=2,
                reason="v3",
                wake_available=False,
                local_capture_available=False,
                ptt_upload_available=True,
                text_available=True,
            ),
        )
        response = asyncio.create_task(
            broadcaster.broadcast_voice("listening", turn_id="T-sequence"),
        )
        release_first_send.set()
        await asyncio.gather(registering, newer, response)
        versions = [
            payload["version"]
            for message in messages
            if message.get("op") == "voice_capability"
            and isinstance((payload := message.get("payload")), dict)
        ]
        assert versions == [2, 3]
        assert max_active_sends == 1

        second_messages: list[dict[str, object]] = []
        second = MagicMock()

        async def _second_send(message: dict[str, object]) -> None:
            second_messages.append(message)

        second.send_json = _second_send
        await broadcaster.register(second)
        assert second_messages[0]["op"] == "voice_capability"
        payload = second_messages[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["version"] == 3

    asyncio.run(_scenario())


class _VoiceBroadcastRecorder:
    """Record ``broadcast_voice_sync`` calls and ``run_turn`` entries in one order.

    Doubles as the ``broadcaster`` and the ``pipeline`` seam of
    :class:`voice_session.DuplexVoiceSession`, so the acceptance can assert that
    ``listening`` reaches the wire before the turn pipeline is ever entered.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.payloads: list[dict[str, object]] = []
        self.calls: list[dict[str, Any]] = []

    def broadcast_voice_sync(
        self,
        phase: str,
        *,
        turn_id: str,
        **payload: object,
    ) -> None:
        self.events.append(("voice", phase, turn_id))
        self.payloads.append(dict(payload))

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(dict(kwargs))
        self.events.append(("run_turn", "", str(kwargs.get("turn_id", ""))))
        return MagicMock()

    @property
    def voice_calls(self) -> list[tuple[str, str]]:
        """Return the ``(phase, turn_id)`` sequence actually broadcast."""
        return [(phase, turn_id) for kind, phase, turn_id in self.events if kind == "voice"]


def _listening_session(
    recorder: _VoiceBroadcastRecorder,
    ingress: voice_audio.AudioIngress,
    wake_engine: _FakeWakeEngine,
    *,
    armed_no_speech_timeout_s: float = 2.0,
) -> voice_session.DuplexVoiceSession:
    return voice_session.DuplexVoiceSession(
        ingress=ingress,
        wake_engine=wake_engine,
        vad=voice_audio.SileroVad(mode="record"),
        pipeline=recorder,
        broadcaster=recorder,
        output_active=lambda: False,
        wake_threshold=0.5,
        config=replace(
            voice_session.RealtimeInputSessionConfig(),
            pre_roll_ms=64,
            min_voiced_s=0.032,
            max_utterance_s=2.0,
            worker_poll_s=0.001,
            shutdown_timeout_s=1.0,
            armed_no_speech_timeout_s=armed_no_speech_timeout_s,
        ),
    )


def test_wake_arm_broadcasts_listening_before_transcribing_on_the_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0006 §5: `listening` is on the wire at arm, before the turn pipeline."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    recorder = _VoiceBroadcastRecorder()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _listening_session(recorder, ingress, _FakeWakeEngine(detections={0}))
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1
        for value in [0, 0, 0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: len(recorder.calls) == 1)
        close = session.close()
    assert close.definitively_closed

    # The recorded broadcast_voice_sync calls themselves, not a call count.
    turn_id = recorder.voice_calls[0][1]
    assert turn_id.startswith("T")
    assert recorder.voice_calls == [("listening", turn_id), ("transcribing", turn_id)]
    # The turn id the pipeline received for the CapturedUtterance is the same T.
    assert recorder.calls[0]["turn_id"] == turn_id
    # `listening` is recorded before run_turn is entered.
    assert recorder.events[0] == ("voice", "listening", turn_id)
    assert recorder.events.index(("voice", "listening", turn_id)) < recorder.events.index(
        ("run_turn", "", turn_id),
    )


def test_broadcast_voice_listening_puts_the_exact_voice_envelope_on_the_socket() -> None:
    """The `listening` wire envelope is `{"op": "voice", "payload": {...}}` verbatim."""

    async def _scenario() -> None:
        broadcaster = InherentBroadcaster()
        messages: list[dict[str, object]] = []

        async def _send_json(message: dict[str, object]) -> None:
            messages.append(message)

        ws = MagicMock()
        ws.send_json = _send_json
        await broadcaster.register(ws)
        await broadcaster.broadcast_voice("listening", turn_id="T-listen")
        assert messages == [
            {"op": "voice", "payload": {"phase": "listening", "turn_id": "T-listen"}},
        ]

    asyncio.run(_scenario())


def test_false_wake_armed_timeout_broadcasts_listening_then_empty_never_transcribing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0014 D27: a wake that never hears speech terminalizes so the card fades."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    recorder = _VoiceBroadcastRecorder()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _listening_session(
            recorder,
            ingress,
            _FakeWakeEngine(detections={0}),
            armed_no_speech_timeout_s=0.064,
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1
        for _ in range(12):
            backend.emit(epoch=epoch, value=0)
            time.sleep(0.002)
        _wait_until(lambda: session.metrics().armed_no_speech_timeouts == 1)
        metrics = session.metrics()
        close = session.close()
    assert close.definitively_closed
    assert metrics.armed_no_speech_timeouts == 1

    turn_id = recorder.voice_calls[0][1]
    assert recorder.voice_calls == [("listening", turn_id), ("empty", turn_id)]
    assert "transcribing" not in [phase for phase, _ in recorder.voice_calls]
    assert recorder.payloads[1] == {"reason": "armed_no_speech_timeout"}
    assert recorder.calls == []


def test_a_second_wake_while_already_armed_broadcasts_no_second_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One armed capture is one visible turn; a redundant detection adds no call."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    recorder = _VoiceBroadcastRecorder()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        # Every wake window detects, so a second detection lands while ARMED.
        session = _listening_session(recorder, ingress, _FakeWakeEngine())
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1
        for value in [0, 0, 0, 0, 10_000, 11_000, 12_000]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        _wait_until(lambda: recorder.voice_calls != [])
        close = session.close()
    assert close.definitively_closed
    listening_calls = [call for call in recorder.voice_calls if call[0] == "listening"]
    assert listening_calls == [("listening", listening_calls[0][1])]


def test_listening_carries_the_armed_turn_id_when_arm_replay_itself_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`arm()` replay can reset the assembler; `listening` still carries that turn."""
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    backend = _FakeBackend()
    ingress = _ingress(backend)
    recorder = _VoiceBroadcastRecorder()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _listening_session(
            recorder,
            ingress,
            _FakeWakeEngine(detections=set()),
            armed_no_speech_timeout_s=0.032,
        )
        assembler = session._assembler
        assembler.prepare()
        # Buffered silence ahead of the wake cursor: arm() replays it through
        # feed(), the armed deadline expires inside arm(), and the assembler
        # resets before arm() ever returns.
        for index in range(3):
            assembler.observe_idle(
                voice_audio.CanonicalAudioFrame(
                    stream_epoch=1,
                    sequence=index,
                    sample_cursor=index * 512,
                    sample_rate_hz=16_000,
                    frame_count=512,
                    adc_time_s=None,
                    captured_monotonic_ns=index,
                    discontinuity_before=False,
                    pcm16_mono=_pcm(0),
                ),
            )
        session._detections.put_nowait(voice_session.WakeDetection(1, 0, 0, 0.9))
        session._drain_detection_commands()
        assert not assembler.armed
        # The assembler's own turn id is already cleared by the replay reset.
        assert assembler.turn_id == ""

    turn_id = recorder.voice_calls[0][1]
    assert turn_id != ""
    assert recorder.voice_calls == [("listening", turn_id), ("empty", turn_id)]
    assert recorder.payloads[1] == {"reason": "armed_no_speech_timeout"}
