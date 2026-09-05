"""ADR-0006 D8/D9 acceptance: the two spoken stages and nothing else.

Three altitudes, one contract.  The router cases drive the candidate window
and the keyword matcher directly, the session cases run the real
``DuplexVoiceSession`` threads so the D9 mode table decides whether a wake hit
during output is a candidate or stays suppressed, and the runtime case
registers real ``ResponseRun``s so the confirmed barge-in is applied under the
policy those runs were started with.

The invariant every case defends: ``output_active`` alone, a wake hit alone,
and any VAD verdict alone can never cancel.  Only a confirmed
``BargeInSignal`` — a second independent spoken signal, an interrupt keyword
inside the window or a PTT press — reaches the interrupt callable, and only a
permitting ``ResponseInterruptPolicy`` turns that into a cancel.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from jarvis.decision.llm import LLMClient
from jarvis.decision.llm_session import LLMSessionFactory
from jarvis.decision.response_run import (
    ResponseRunRegistry,
    legacy_full_text_policy,
    start_response_run,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import (
    JarvisRuntime,
    _wave4_response_flags,
    make_barge_in_interrupt_callable,
)
from jarvis.shared.realtime import (
    RESPONSE_CANCEL_REASONS,
    Wave1FeatureFlags,
    new_response_id,
)
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.committed_event_bus import CommittedEventBus
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_interrupt,
    voice_session,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared.realtime_trace import RealtimeTracePoint

_FRAME = voice_audio.SILERO_CHUNK_SAMPLES
_SPEECH = 10_000


def _pcm(value: int) -> bytes:
    return np.full(_FRAME, value, dtype="<i2").tobytes()


def _frame(index: int, value: int) -> voice_audio.CanonicalAudioFrame:
    return voice_audio.CanonicalAudioFrame(
        stream_epoch=1,
        sequence=index,
        sample_cursor=index * _FRAME,
        sample_rate_hz=16_000,
        frame_count=_FRAME,
        adc_time_s=None,
        captured_monotonic_ns=index,
        discontinuity_before=False,
        pcm16_mono=_pcm(value),
    )


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.001)


def _traces(name: str) -> list[RealtimeTracePoint]:
    return [point for point in realtime_trace_snapshot() if point.name == name]


class _RecordingInterrupt:
    """The injected interrupt seam; records every confirmed barge-in call."""

    def __init__(self, outcome: str = "cancelled") -> None:
        self.calls: list[str] = []
        self._outcome = outcome

    def __call__(self, confirm_source: str) -> str:
        self.calls.append(confirm_source)
        return self._outcome


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def run(self, _output_names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


class _ScriptedDecoder:
    """Return scripted partial hypotheses in order; the last one repeats."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.calls = 0

    def partial_text(self, audio_bytes: bytes) -> str:
        del audio_bytes
        index = min(self.calls, len(self._texts) - 1)
        self.calls += 1
        return self._texts[index]


def _router(
    interrupt: _RecordingInterrupt,
    *,
    output_active: bool = True,
    candidate_window_ms: int = 1200,
    keywords: tuple[str, ...] = ("停", "别说了", "stop"),
) -> voice_interrupt.BargeInRouter:
    return voice_interrupt.BargeInRouter(
        config=voice_interrupt.BargeInConfig(
            enabled=True,
            candidate_window_ms=candidate_window_ms,
            confirm_timeout_ms=800,
            interrupt_keywords=keywords,
        ),
        interrupt=interrupt,
        output_active=lambda: output_active,
        session_id="S-barge",
    )


class _PartialHarness:
    """One armed assembler whose partial revisions reach a barge-in router."""

    def __init__(self, decoder: _ScriptedDecoder, router: voice_interrupt.BargeInRouter) -> None:
        self._patch = patch.object(
            voice_audio,
            "_load_silero_session",
            return_value=_EnergySession(),
        )
        self._patch.start()
        vad = voice_audio.SileroVad(mode="record")
        vad._t = voice_audio.VadThresholds(0.4, -45.0, 1, 1, 10)  # noqa: SLF001
        self.lane = voice_session.PartialAsrLane(decoder)
        self.assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                min_voiced_s=0.032,
                partial_asr=voice_session.PartialAsrConfig(
                    enabled=True,
                    interval_ms=32,
                    candidate_ms=64,
                    max_hold_ms=160,
                    post_roll_ms=32,
                ),
            ),
            sample_rate_hz=16_000,
            frame_samples=_FRAME,
            session_id="S-barge",
            lane=self.lane,
            barge_in=router,
        )
        self.assembler.prepare()
        self.assembler.arm(voice_session.WakeDetection(1, 0, 0, 0.9))
        self.index = 0

    def close(self) -> None:
        self._patch.stop()

    def feed(self, value: int = _SPEECH) -> None:
        self.assembler.feed(_frame(self.index, value))
        self.index += 1
        self.lane.run_once(timeout_s=0)


# --- router: candidate window, keyword confirm, PTT confirm -----------------


def test_interrupt_keyword_inside_the_window_confirms_exactly_once() -> None:
    """Case 2/5: the keyword confirms once; later revisions cannot confirm again."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt)
    harness = _PartialHarness(_ScriptedDecoder(["请", "请停下", "请停下"]), router)
    try:
        router.open_candidate(stream_epoch=1, input_sample_cursor=0, probability=0.9)
        assert interrupt.calls == []
        for _ in range(8):
            harness.feed()
    finally:
        harness.close()

    confirmed = _traces("barge_in_confirmed")
    assert len(confirmed) == 1, [point.attributes for point in confirmed]
    assert confirmed[0].attributes["confirm_source"] == "keyword"
    assert confirmed[0].attributes["keyword"] == "停"
    assert confirmed[0].attributes["outcome"] == "cancelled"
    assert interrupt.calls == ["keyword"]
    assert router.confirmations == 1
    assert len(_traces("barge_in_candidate")) == 1
    assert _traces("barge_in_candidate_dropped") == []


def test_a_keyword_with_no_open_window_never_confirms() -> None:
    """The keyword is only the SECOND stage; alone it is ordinary speech."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt)
    harness = _PartialHarness(_ScriptedDecoder(["停"]), router)
    try:
        for _ in range(6):
            harness.feed()
    finally:
        harness.close()

    assert interrupt.calls == []
    assert _traces("barge_in_confirmed") == []
    assert router.confirmations == 0


def test_a_window_that_elapses_without_a_keyword_is_dropped_as_telemetry() -> None:
    """Case 3: candidate_window_ms with no keyword cancels nothing (F11)."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt, candidate_window_ms=1)
    router.open_candidate(stream_epoch=1, input_sample_cursor=0, probability=0.9)
    time.sleep(0.01)
    router.expire_due()

    dropped = _traces("barge_in_candidate_dropped")
    assert len(dropped) == 1
    assert dropped[0].attributes["reason"] == "candidate_window_elapsed"
    assert router.candidates_dropped == 1
    assert interrupt.calls == []
    assert _traces("barge_in_confirmed") == []
    # And the keyword can no longer confirm the window it missed.
    assert router.offer_partial("停") is None
    assert interrupt.calls == []


def test_a_second_wake_hit_supersedes_the_open_window_and_still_counts_it() -> None:
    """F11 counts every false candidate, replaced ones included."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt)
    router.open_candidate(stream_epoch=1, input_sample_cursor=0, probability=0.9)
    router.open_candidate(stream_epoch=1, input_sample_cursor=1280, probability=0.9)

    dropped = _traces("barge_in_candidate_dropped")
    assert len(dropped) == 1
    assert dropped[0].attributes["reason"] == "superseded_by_candidate"
    assert router.candidates == 2
    assert router.candidates_dropped == 1
    assert interrupt.calls == []


def test_a_ptt_upload_during_output_confirms_with_no_prior_candidate() -> None:
    """Case 4: a button press is not echo, so it needs no first stage."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt)

    assert router.confirm_ptt() == "cancelled"

    confirmed = _traces("barge_in_confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].attributes["confirm_source"] == "ptt"
    assert interrupt.calls == ["ptt"]
    assert _traces("barge_in_candidate") == []


def test_a_ptt_upload_while_nothing_is_speaking_is_not_a_barge_in() -> None:
    """``output_active`` is the only gate a press still has to pass."""
    reset_realtime_trace()
    interrupt = _RecordingInterrupt()
    router = _router(interrupt, output_active=False)

    assert router.confirm_ptt() == "output_idle"
    assert interrupt.calls == []
    assert _traces("barge_in_confirmed") == []


def test_an_interrupt_callable_that_never_returns_is_bounded() -> None:
    """``confirm_timeout_ms`` is the outer bound on the injected callable."""
    reset_realtime_trace()
    started = time.monotonic()

    def _hang(_confirm_source: str) -> str:
        time.sleep(5.0)
        return "cancelled"

    router = voice_interrupt.BargeInRouter(
        config=voice_interrupt.BargeInConfig(enabled=True, confirm_timeout_ms=50),
        interrupt=_hang,
        output_active=lambda: True,
        session_id="S-barge",
    )
    assert router.confirm_ptt() == "timeout"
    assert time.monotonic() - started < 2.0
    assert _traces("barge_in_confirmed")[0].attributes["outcome"] == "timeout"


# --- session: the D9 mode table decides whether a wake hit is a candidate ---


class _FakeBackend:
    """Callback-capable backend with one owner and no physical device."""

    def __init__(self) -> None:
        self.format = voice_backend.AudioInputFormat(16_000, 1, _FRAME)
        self.sinks: dict[int, voice_backend.InputFrameSink] = {}
        self.attempts: dict[int, str] = {}
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.version = 0

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: voice_backend.InputFrameSink,
        render_source: voice_backend.RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStartResult:
        del render_source, timeout_s
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.version += 1
        self.sinks[stream_epoch] = frame_sink
        self.attempts[stream_epoch] = attempt_id
        return voice_backend.BackendStartResult(
            status=voice_backend.BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=voice_backend.InputDeviceProfile(
                device_uid="fake-input",
                device_name="fake-input",
                backend="fake",
                input_format=self.format,
            ),
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
        if self.active_epoch == stream_epoch and self.active_attempt_id == attempt_id:
            self.active_epoch = None
            self.active_attempt_id = None
            self.version += 1
        return voice_backend.BackendStopResult(
            status=voice_backend.BackendStopStatus.CLOSED,
            stream_epoch=stream_epoch,
            attempt_id=attempt_id,
        )

    def emit(self, *, epoch: int, value: int) -> None:
        self.sinks[epoch](
            stream_epoch=epoch,
            attempt_id=self.attempts[epoch],
            callback_buffer=bytearray(_pcm(value)),
            frame_count=_FRAME,
            adc_time_s=time.monotonic(),
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=False,
        )

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        del stream_epoch
        return None

    def current_device_uid(self) -> str | None:
        return "fake-input"

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
        closed = self.active_epoch is None
        return voice_backend.BackendOwnershipSnapshot(
            state=(
                voice_backend.BackendLifecycleState.CLOSED
                if closed
                else voice_backend.BackendLifecycleState.OPEN
            ),
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.version,
            physical_owner_possible=not closed,
            helper_thread_alive=False,
        )


class _FakeWakeEngine:
    model_name = "hey_jarvis_v0.1"

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        call = self.calls
        self.calls += 1
        return {self.model_name: 0.9 if call == 0 else 0.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingPipeline:
    """Final ASR never runs in these cases; partial decode is scripted."""

    def __init__(self, texts: list[str] | None = None) -> None:
        self.calls: list[str] = []
        self._decoder = _ScriptedDecoder(texts if texts is not None else ["请停下"])

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401 - adapter signature
        self.calls.append(str(kwargs.get("turn_id")))
        return MagicMock()

    def prewarm_input_model(self) -> None:
        return None

    def partial_text(self, audio_bytes: bytes) -> str:
        return self._decoder.partial_text(audio_bytes)


_NATURAL_PROFILE = voice_backend.DeviceProfileKey(
    input_uid="fake-input",
    output_uid=None,
    backend="fake",
    route_kind=voice_backend.RouteKind.HEADPHONES,
    input_sample_rate=16_000,
    output_sample_rate=None,
)


def _session(  # noqa: PLR0913 - one composition root per D9 mode/config case
    backend: _FakeBackend,
    *,
    detection_mode: str,
    interrupt: _RecordingInterrupt | None,
    barge_in_enabled: bool = True,
    partial_asr_enabled: bool = True,
    natural: bool = False,
    partial_texts: list[str] | None = None,
) -> voice_session.DuplexVoiceSession:
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            native_ring_capacity=64,
            default_subscriber_capacity=32,
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            shutdown_timeout_s=1.0,
            barge_detection_mode=detection_mode,
            accepted_natural_profiles=(_NATURAL_PROFILE,) if natural else (),
        ),
    )
    return voice_session.DuplexVoiceSession(
        ingress=ingress,
        wake_engine=_FakeWakeEngine(),
        vad=voice_audio.SileroVad(mode="record"),
        pipeline=_RecordingPipeline(partial_texts if partial_texts is not None else ["你好"]),
        broadcaster=None,
        output_active=lambda: True,
        wake_threshold=0.5,
        config=replace(
            voice_session.RealtimeInputSessionConfig(),
            worker_poll_s=0.001,
            shutdown_timeout_s=1.0,
            partial_asr=voice_session.PartialAsrConfig(enabled=partial_asr_enabled),
            barge_in=voice_interrupt.BargeInConfig(
                enabled=barge_in_enabled,
                candidate_window_ms=1200,
                confirm_timeout_ms=800,
            ),
        ),
        barge_in_interrupt=interrupt,
    )


def test_a_wake_hit_during_output_opens_a_candidate_and_arms_but_cancels_nothing() -> None:
    """Case 1: stage one arms capture; on its own it can never cancel."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(
            backend,
            detection_mode="keyword_two_stage",
            interrupt=interrupt,
        )
        assert session.start().started
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().barge_in_candidates > 0)
        # Capture only starts once armed, so speech has to keep arriving after
        # the candidate: this is the arming the candidate bought.
        for _ in range(24):
            backend.emit(epoch=epoch, value=_SPEECH)
            time.sleep(0.002)
        _wait_until(lambda: session.metrics().capture_starts > 0)
        metrics = session.metrics()
        assert session.close().definitively_closed

    assert metrics.barge_in_candidates == 1
    assert metrics.wake_suppressed_during_output == 0
    assert interrupt.calls == []
    candidate = _traces("barge_in_candidate")
    assert len(candidate) == 1
    assert candidate[0].attributes["allowed_barge_mode"] == "keyword_two_stage"
    assert _traces("barge_in_confirmed") == []


def test_the_whole_session_path_confirms_once_on_the_spoken_interrupt_keyword() -> None:
    """Both stages through the real threads: wake during output, then "停"."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(
            backend,
            detection_mode="keyword_two_stage",
            interrupt=interrupt,
            partial_texts=["请停下"],
        )
        assert session.start().started
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().barge_in_candidates > 0)
        for _ in range(24):
            backend.emit(epoch=epoch, value=_SPEECH)
            time.sleep(0.002)
        _wait_until(lambda: session.metrics().barge_in_confirmations > 0)
        for _ in range(24):
            backend.emit(epoch=epoch, value=_SPEECH)
            time.sleep(0.002)
        metrics = session.metrics()
        assert session.close().definitively_closed

    assert interrupt.calls == ["keyword"]
    assert metrics.barge_in_candidates == 1
    assert metrics.barge_in_confirmations == 1
    confirmed = _traces("barge_in_confirmed")
    assert len(confirmed) == 1
    assert confirmed[0].attributes["confirm_source"] == "keyword"
    assert confirmed[0].attributes["keyword"] == "停"


def test_ptt_mode_keeps_todays_suppression_and_no_keyword_can_confirm() -> None:
    """Case 6: with the D9 ceiling at ptt the wake hit is dropped as before."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(backend, detection_mode="ptt", interrupt=interrupt)
        assert session.start().started
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().wake_suppressed_during_output > 0)
        metrics = session.metrics()
        assert session.close().definitively_closed

    assert metrics.barge_in_candidates == 0
    assert metrics.capture_starts == 0
    assert interrupt.calls == []
    suppressed = _traces("audio_input_wake_suppressed")
    assert suppressed
    assert suppressed[0].attributes["reason"] == "wave3_no_interrupt_policy_during_output"
    assert suppressed[0].attributes["hard_cancel_performed"] is False
    assert _traces("barge_in_candidate") == []
    # A PTT press still confirms in ptt mode — that is the whole point of D9.
    assert session.confirm_ptt_barge_in() == "cancelled"
    assert interrupt.calls == ["ptt"]


def test_natural_mode_is_downgraded_to_keyword_two_stage_with_one_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Case 7: natural stays unreachable; the downgrade warns once."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    caplog.set_level(logging.WARNING, logger="jarvis.surface.voice_session")
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(
            backend,
            detection_mode="ptt",
            interrupt=interrupt,
            natural=True,
        )
        assert session.start().started
        profile = session.device_profile
        assert profile is not None
        assert profile.allowed_barge_mode == "natural"
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().barge_in_candidates > 0)
        metrics = session.metrics()
        assert session.close().definitively_closed

    warnings = [
        record for record in caplog.records if "natural is unreachable" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert metrics.barge_in_candidates == 1
    assert interrupt.calls == []
    assert _traces("barge_in_candidate")[0].attributes["allowed_barge_mode"] == "natural"


def test_keyword_mode_without_partial_asr_fails_closed_to_ptt_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No partial lane means no keyword confirm, so no candidate is opened."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    caplog.set_level(logging.WARNING, logger="jarvis.surface.voice_session")
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(
            backend,
            detection_mode="keyword_two_stage",
            interrupt=interrupt,
            partial_asr_enabled=False,
        )
        assert session.start().started
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().wake_suppressed_during_output > 0)
        metrics = session.metrics()
        assert session.confirm_ptt_barge_in() == "cancelled"
        assert session.close().definitively_closed

    assert metrics.barge_in_candidates == 0
    assert [r for r in caplog.records if "needs partial_asr.enabled" in r.getMessage()]
    assert interrupt.calls == ["ptt"]


def test_barge_in_disabled_builds_no_router_and_changes_nothing() -> None:
    """Case 8b: with enabled false the session behaves exactly as it did."""
    reset_realtime_trace()
    backend = _FakeBackend()
    interrupt = _RecordingInterrupt()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = _session(
            backend,
            detection_mode="keyword_two_stage",
            interrupt=interrupt,
            barge_in_enabled=False,
        )
        assert session.start().started
        epoch = backend.active_epoch
        assert epoch is not None
        for value in range(12):
            backend.emit(epoch=epoch, value=value + 1)
        _wait_until(lambda: session.metrics().wake_suppressed_during_output > 0)
        metrics = session.metrics()
        assert not session.barge_in_armed
        assert session.confirm_ptt_barge_in() == "disabled"
        assert session.close().definitively_closed

    assert metrics.barge_in_candidates == 0
    assert metrics.barge_in_confirmations == 0
    assert metrics.capture_starts == 0
    assert interrupt.calls == []
    assert _traces("barge_in_candidate") == []
    assert _traces("barge_in_confirmed") == []


def test_the_barge_in_config_block_defaults_off_and_parses() -> None:
    """The config is read out of the same YAML block device-profile owns."""
    assert voice_session.RealtimeInputSessionConfig().barge_in == voice_interrupt.BargeInConfig()
    assert not voice_interrupt.BargeInConfig().enabled
    parsed = voice_session.realtime_input_session_config_from_mapping(
        {
            "barge_in": {
                "detection_mode": "keyword_two_stage",
                "accepted_natural_profiles": [],
                "enabled": True,
                "candidate_window_ms": 900,
                "confirm_timeout_ms": 700,
                "interrupt_keywords": ["停", "stop"],
            },
        },
    ).barge_in
    assert parsed == voice_interrupt.BargeInConfig(
        enabled=True,
        candidate_window_ms=900,
        confirm_timeout_ms=700,
        interrupt_keywords=("停", "stop"),
    )
    with pytest.raises(ValueError, match="candidate_window_ms"):
        voice_session.realtime_input_session_config_from_mapping(
            {"barge_in": {"candidate_window_ms": 0}},
        )


# --- runtime: the run's own interrupt policy decides -------------------------


_LLM_CONFIG: dict[str, Any] = {
    "provider": "openai",
    "model": "synthetic",
    "base_url": "https://example.invalid",
    "max_tokens": 128,
}


def _runtime(tmp_path: Path) -> JarvisRuntime:
    """Assemble the runtime slice the barge-in interrupt callable reads."""
    paths = bootstrap_runtime(tmp_path)
    config: dict[str, Any] = {
        "realtime": {
            "enabled": True,
            "concurrency_safety": {
                "transactional_event_append": True,
                "lifecycle_terminal_cas": True,
                "confirmation_dispatch_outbox": False,
                "exactly_once_cost_accounting": False,
            },
            "response": {
                "response_run_lifecycle": True,
                "independent_response_cancel": True,
                "cancel_timeout_ms": 500,
            },
        },
    }
    flags = _wave4_response_flags(config)
    return JarvisRuntime(
        config=config,
        runtime_paths=paths,
        conn=open_event_log(paths.event_log),
        tool_registry=build_default_registry(),
        lifecycle=ActionLifecycle(),
        llm_client=LLMClient(_LLM_CONFIG),
        system_prompt="",
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True,
            lifecycle_terminal_cas=True,
        ),
        response_flags=flags,
        llm_session_factory=LLMSessionFactory(_LLM_CONFIG),
        response_runs=ResponseRunRegistry(),
        committed_event_bus=CommittedEventBus(),
    )


def _open_run(runtime: JarvisRuntime, turn_id: str, *, confirmed_playback: str) -> str:
    factory = runtime.llm_session_factory
    assert factory is not None
    registry = runtime.response_runs
    assert registry is not None
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
            evidence_snapshot_hash="a" * 64,
            preset_snapshot_hash="b" * 64,
        ),
        response_id=response_id,
        confirmed_playback="ignore" if confirmed_playback == "ignore" else (
            "interrupt_expected_playback_generation"
        ),
    )
    registry.register(run)
    return response_id


def _reasons(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT json_extract(payload_json, '$.reason') FROM events"
        " WHERE type = 'response.cancelled' ORDER BY id",
    ).fetchall()
    return [str(row[0]) for row in rows]


def test_a_permitting_run_is_cancelled_with_the_barge_in_reason(tmp_path: Path) -> None:
    """The confirmed interrupt reaches the existing generation-scope cancel."""
    runtime = _runtime(tmp_path)
    response_id = _open_run(
        runtime,
        "T-barge",
        confirmed_playback="interrupt_expected_playback_generation",
    )
    interrupt = make_barge_in_interrupt_callable(runtime)

    assert interrupt("keyword") == "cancelled"

    assert _reasons(runtime.conn) == ["barge_in"]
    payload = runtime.conn.execute(
        "SELECT json_extract(payload_json, '$.response_id'),"
        " json_extract(payload_json, '$.cancel_scope') FROM events"
        " WHERE type = 'response.cancelled'",
    ).fetchone()
    assert payload[0] == response_id
    assert payload[1] == "generation"
    runtime.conn.close()


def test_a_run_whose_policy_ignores_playback_is_never_cancelled(tmp_path: Path) -> None:
    """Case 8: the runtime applies the policy the run was started with."""
    runtime = _runtime(tmp_path)
    _open_run(runtime, "T-ignore", confirmed_playback="ignore")
    interrupt = make_barge_in_interrupt_callable(runtime)

    assert interrupt("keyword") == "policy_ignore"

    assert _reasons(runtime.conn) == []
    runtime.conn.close()


def test_zero_or_ambiguous_open_runs_cancel_nothing(tmp_path: Path) -> None:
    """The runtime never invents a target when it cannot name exactly one."""
    runtime = _runtime(tmp_path)
    interrupt = make_barge_in_interrupt_callable(runtime)
    assert interrupt("ptt") == "no_open_run"

    _open_run(runtime, "T-a", confirmed_playback="interrupt_expected_playback_generation")
    _open_run(runtime, "T-b", confirmed_playback="interrupt_expected_playback_generation")
    assert interrupt("ptt") == "ambiguous_open_runs"

    assert _reasons(runtime.conn) == []
    runtime.conn.close()


def test_the_confirmed_playback_default_now_permits_the_interrupt(tmp_path: Path) -> None:
    """A run started without an explicit policy value permits a barge-in."""
    runtime = _runtime(tmp_path)
    factory = runtime.llm_session_factory
    registry = runtime.response_runs
    assert factory is not None
    assert registry is not None
    trigger = emit_event(
        runtime.conn,
        type="utterance.received",
        payload={"transcript": "讲个长故事", "turn_id": "T-default"},
    )
    response_id = new_response_id()
    run = start_response_run(
        runtime.conn,
        turn_id="T-default",
        trigger_event_uid=trigger.event_uid,
        request_client=factory.create(factory.snapshot(), response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash="a" * 64,
            preset_snapshot_hash="b" * 64,
        ),
        response_id=response_id,
    )
    assert run.interrupt_policy.confirmed_playback == "interrupt_expected_playback_generation"
    assert run.interrupt_policy.generation_action == "cancel"
    assert run.interrupt_policy.action_action == "never"
    runtime.conn.close()


def test_the_barge_in_reason_is_inside_the_closed_cancel_vocabulary() -> None:
    """Without this the durable row would be normalized to operator_request."""
    assert "barge_in" in RESPONSE_CANCEL_REASONS
    assert voice_asr.normalize_partial_text("STOP ") == "stop"
