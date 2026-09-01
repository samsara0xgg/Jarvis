"""ADR-0006 Wave 3 integration acceptance for one continuous audio ingress."""

# ruff: noqa: SLF001 - private owner/ring seams are the race surface under test.
from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.runtime import inherent_loop
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_media,
    voice_session,
)

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
        self.active_epoch: int | None = None
        self.device_uid = "fake-input-A"
        self.pending_fault: voice_backend.BackendFault | None = None
        self.start_count = 0
        self.stop_count = 0
        self.active_owner_count = 0
        self.max_active_owner_count = 0

    def start(
        self,
        *,
        stream_epoch: int,
        frame_sink: voice_backend.InputFrameSink,
        render_source: voice_backend.RenderSource | None = None,
    ) -> voice_backend.BackendStartResult:
        assert render_source is None
        assert self.active_epoch is None, "a second physical owner was opened"
        self.start_count += 1
        self.active_epoch = stream_epoch
        self.active_owner_count += 1
        self.max_active_owner_count = max(
            self.max_active_owner_count,
            self.active_owner_count,
        )
        self.sinks[stream_epoch] = frame_sink
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
        )

    def stop(self, *, stream_epoch: int) -> voice_backend.BackendStopResult:
        self.stop_count += 1
        if self.active_epoch == stream_epoch:
            self.active_epoch = None
            if self.stop_status is voice_backend.BackendStopStatus.CLOSED:
                self.active_owner_count -= 1
        return voice_backend.BackendStopResult(
            status=self.stop_status,
            stream_epoch=stream_epoch,
            reason=(
                "injected_stuck_backend"
                if self.stop_status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
                else None
            ),
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


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
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
        callback_buffer: Any,  # noqa: ANN401 - mirrors untyped sounddevice buffer
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
    ) -> None:
        del (
            stream_epoch,
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
        assert first.start(stream_epoch=1, frame_sink=_sink).started
        busy = second.start(stream_epoch=1, frame_sink=_sink)
        assert busy.status is voice_backend.BackendStartStatus.OWNER_BUSY
        streams[0].callback(
            bytearray(_pcm(123)),
            512,
            MagicMock(inputBufferAdcTime=1.25),
            MagicMock(input_overflow=False, __bool__=lambda _self: False),
        )
        assert copied == [_pcm(123)]
        assert first.stop(stream_epoch=1).definitively_closed
        assert second.start(stream_epoch=2, frame_sink=_sink).started
        assert second.stop(stream_epoch=2).definitively_closed
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)


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
        failed = backend.start(stream_epoch=1, frame_sink=MagicMock())
        assert failed.status is voice_backend.BackendStartStatus.FAILED_CLOSED
        assert streams[0].closed
        assert successor.start(stream_epoch=2, frame_sink=MagicMock()).started
        assert successor.stop(stream_epoch=2).definitively_closed


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
        timed_out = timed_out_backend.start(stream_epoch=1, frame_sink=MagicMock())
        assert first_open_entered.is_set()
        assert timed_out.status is voice_backend.BackendStartStatus.OPEN_UNCERTAIN
        assert (
            successor.start(stream_epoch=2, frame_sink=MagicMock()).status
            is voice_backend.BackendStartStatus.OWNER_BUSY
        )
        release_first_open.set()
        _wait_until(lambda: bool(streams) and streams[0].closed)
        assert successor.start(stream_epoch=3, frame_sink=MagicMock()).started
        assert successor.stop(stream_epoch=3).definitively_closed
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
        assert first.start(stream_epoch=1, frame_sink=MagicMock()).started
        close = first.stop(stream_epoch=1)
        assert close_entered.is_set()
        assert close.status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
        assert (
            successor.start(stream_epoch=2, frame_sink=MagicMock()).status
            is voice_backend.BackendStartStatus.OWNER_BUSY
        )
        release_close.set()
        _wait_until(lambda: streams[0].closed)
        assert successor.start(stream_epoch=3, frame_sink=MagicMock()).started
        assert successor.stop(stream_epoch=3).definitively_closed
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

        def stop(self, *, stream_epoch: int) -> voice_backend.BackendStopResult:
            self.stop_entered.set()
            assert self.release_stop.wait(timeout=0.5)
            return super().stop(stream_epoch=stream_epoch)

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
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
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


def test_runtime_prewarms_sensevoice_before_any_input_owner_starts(tmp_path: Path) -> None:
    """Model/session cold start belongs to daemon construction, not first speech."""
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
    recognizer.prewarm.assert_called_once_with()
    assert pipeline._recognizer is recognizer


def test_real_concurrent_churn_1000_epoch_subscribe_fanout_close_cycles() -> None:
    """1,000 real thread/callback cycles show no wrap, epoch, deadlock, or leak."""
    backend = _FakeBackend()
    ingress = _ingress(backend, native_capacity=8, subscriber_capacity=2)
    assert ingress.start().started
    retained: list[bytes] = []
    for cycle in range(1_000):
        subscription = ingress.subscribe(
            name=f"churn-{cycle}",
            purpose=voice_audio.SubscriberPurpose.CAPTURE,
            capacity=2,
        )
        epoch = ingress.stream_epoch
        assert epoch is not None
        backend.emit(epoch=epoch, value=cycle % 30_000)
        frame = _read_frames(subscription, 1)[0]
        assert frame.stream_epoch == epoch
        assert frame.sample_cursor == 0
        if cycle < 8:
            retained.append(frame.pcm16_mono)
        subscription.close()
        stopped = ingress.stop_for_sleep()
        assert stopped is not None
        assert stopped.definitively_closed
        reopened = ingress.resume_after_wake()
        assert reopened is not None
        assert reopened.started
        # The old callback closure remains callable but must never publish.
        backend.emit(epoch=epoch, value=31_000)
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
