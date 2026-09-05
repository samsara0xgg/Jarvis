"""ADR-0006 D9 device profile resolver and route observer acceptance."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import wave
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.surface import voice_audio, voice_backend
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.voice_backend import DeviceProfileKey, OutputRoute, RouteKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_INPUT_FORMAT = voice_backend.AudioInputFormat(16_000, 1, 512)
_INPUT_PROFILE = voice_backend.InputDeviceProfile(
    device_uid="sounddevice:3:MacBook Pro Microphone",
    device_name="MacBook Pro Microphone",
    backend="sounddevice",
    input_format=_INPUT_FORMAT,
)
_SPEAKERS = OutputRoute("BuiltInSpeakerDevice", "MacBook Pro Speakers", "bltn", "ispk", 48_000)
_JACK = OutputRoute("BuiltInHeadphoneOutputDevice", "External Headphones", "bltn", "hdpn", 48_000)
_JACK_KEY = DeviceProfileKey(
    input_uid=_INPUT_PROFILE.device_uid,
    output_uid=_JACK.uid,
    backend="sounddevice",
    route_kind=RouteKind.HEADPHONES,
    input_sample_rate=16_000,
    output_sample_rate=48_000,
)


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.001)


@pytest.mark.parametrize(
    ("input_transport", "output_transport", "output_data_source", "name", "expected"),
    [
        ("bltn", "bltn", "ispk", "MacBook Pro Speakers", RouteKind.SPEAKER),
        ("bltn", "bltn", "hdpn", "External Headphones", RouteKind.HEADPHONES),
        ("blue", "blue", None, "AirPods Pro", RouteKind.UNKNOWN),
        ("usb ", "usb ", None, "USB Headset", RouteKind.UNKNOWN),
        ("bltn", "dprt", None, "BenQ PD2700U", RouteKind.UNKNOWN),
        ("bltn", "virt", None, "BlackHole 16ch", RouteKind.UNKNOWN),
        ("bltn", "hdmi", None, "LG TV", RouteKind.UNKNOWN),
        ("bltn", None, None, "no output", RouteKind.UNKNOWN),
    ],
)
def test_route_kind_follows_transport_not_name(
    input_transport: str,
    output_transport: str | None,
    output_data_source: str | None,
    name: str,
    expected: RouteKind,
) -> None:
    """Only the built-in headphone jack earns headphones; names grant nothing."""
    del input_transport, name
    assert (
        voice_backend.route_kind_for(
            output_transport=output_transport,
            output_data_source=output_data_source,
        )
        is expected
    )


@pytest.mark.parametrize("detection_mode", ["ptt", "natural", "bogus", ""])
@pytest.mark.parametrize("output", [_SPEAKERS, None, replace(_SPEAKERS, transport_type="usb ")])
def test_speaker_and_unknown_routes_fail_closed_to_ptt(
    detection_mode: str,
    output: OutputRoute | None,
) -> None:
    """Anything but keyword_two_stage resolves ptt; natural is unreachable."""
    snapshot = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=output,
        stream_epoch=1,
        detection_mode=detection_mode,
        accepted_natural_profiles=(),
    )
    assert snapshot.allowed_barge_mode == "ptt"
    assert snapshot.key.route_kind in {RouteKind.SPEAKER, RouteKind.UNKNOWN}
    assert snapshot.key.aec_mode == "none"


def test_keyword_two_stage_only_when_configured_and_listed_jack_resolves_natural() -> None:
    """The config ceiling applies to speakers; natural needs the exact listed key."""
    keyword = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=_SPEAKERS,
        stream_epoch=1,
        detection_mode="keyword_two_stage",
        accepted_natural_profiles=(_JACK_KEY,),
    )
    assert keyword.allowed_barge_mode == "keyword_two_stage"
    unlisted_jack = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=_JACK,
        stream_epoch=1,
        detection_mode="ptt",
        accepted_natural_profiles=(),
    )
    assert unlisted_jack.key.route_kind is RouteKind.HEADPHONES
    assert unlisted_jack.allowed_barge_mode == "ptt"
    listed_jack = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=_JACK,
        stream_epoch=2,
        detection_mode="ptt",
        accepted_natural_profiles=(_JACK_KEY,),
    )
    assert listed_jack.allowed_barge_mode == "natural"
    assert listed_jack.key == _JACK_KEY
    assert listed_jack.stream_epoch == 2
    other_mic = voice_backend.resolve_device_profile(
        input_profile=replace(_INPUT_PROFILE, device_uid="sounddevice:0:allen Microphone"),
        output=_JACK,
        stream_epoch=2,
        detection_mode="ptt",
        accepted_natural_profiles=(_JACK_KEY,),
    )
    assert other_mic.allowed_barge_mode == "ptt"


def test_listed_profiles_never_promote_an_observed_speaker() -> None:
    """A listing lifts unknown to headphones/natural; an observed speaker stays ptt."""
    speaker_key = replace(_JACK_KEY, output_uid=_SPEAKERS.uid)
    listed_speaker = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=_SPEAKERS,
        stream_epoch=1,
        detection_mode="ptt",
        accepted_natural_profiles=(speaker_key,),
    )
    assert listed_speaker.key.route_kind is RouteKind.SPEAKER
    assert listed_speaker.allowed_barge_mode == "ptt"
    usb = OutputRoute("USB-DAC-UID", "USB Headset", "usb ", None, 48_000)
    listed_unknown = voice_backend.resolve_device_profile(
        input_profile=_INPUT_PROFILE,
        output=usb,
        stream_epoch=1,
        detection_mode="ptt",
        accepted_natural_profiles=(replace(_JACK_KEY, output_uid=usb.uid),),
    )
    assert listed_unknown.key.route_kind is RouteKind.HEADPHONES
    assert listed_unknown.allowed_barge_mode == "natural"


def _key_mapping(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "input_uid": _JACK_KEY.input_uid,
        "output_uid": _JACK_KEY.output_uid,
        "backend": "sounddevice",
        "route_kind": "headphones",
        "aec_mode": "none",
        "input_sample_rate": 16_000,
        "output_sample_rate": 48_000,
    }
    base.update(overrides)
    return base


def test_config_defaults_and_fail_closed_parsing() -> None:
    """Keys ship off/ptt/empty; malformed values never widen the barge ceiling."""
    defaults = voice_audio.audio_ingress_config_from_mapping({})
    assert defaults.route_observer_enabled is False
    assert defaults.barge_detection_mode == "ptt"
    assert defaults.accepted_natural_profiles == ()
    parsed = voice_audio.audio_ingress_config_from_mapping(
        {
            "route_observer": {"enabled": True},
            "barge_in": {
                "detection_mode": "keyword_two_stage",
                "accepted_natural_profiles": [
                    _key_mapping(),
                    _key_mapping(route_kind="speaker"),
                    _key_mapping(aec_mode="software"),
                    {"input_uid": "x"},
                    "not-a-mapping",
                ],
            },
        },
    )
    assert parsed.route_observer_enabled is True
    assert parsed.barge_detection_mode == "keyword_two_stage"
    assert parsed.accepted_natural_profiles == (_JACK_KEY,)
    lenient = voice_audio.audio_ingress_config_from_mapping(
        {"barge_in": {"detection_mode": "natural", "accepted_natural_profiles": "nope"}},
    )
    assert lenient.barge_detection_mode == "ptt"
    assert lenient.accepted_natural_profiles == ()
    with pytest.raises(ValueError, match=r"route_observer\.enabled"):
        voice_audio.audio_ingress_config_from_mapping({"route_observer": {"enabled": "yes"}})
    with pytest.raises(ValueError, match="route_observer must be a mapping"):
        voice_audio.audio_ingress_config_from_mapping({"route_observer": True})


def test_no_backend_claims_aec_or_natural_barge_in() -> None:
    """Regression pin: capability claims stay False on the production backend."""
    capabilities = voice_backend.SoundDeviceDuplexBackend(
        input_format=_INPUT_FORMAT,
        open_timeout_s=0.5,
        close_timeout_s=0.5,
    ).capabilities()
    assert capabilities.aec is False
    assert capabilities.natural_barge_in is False


def test_profile_key_text_has_seven_fields() -> None:
    """The trace form carries every D9 field in order."""
    assert _JACK_KEY.as_text() == (
        "sounddevice:3:MacBook Pro Microphone|BuiltInHeadphoneOutputDevice|sounddevice|"
        "headphones|none|16000|48000"
    )



class _RouteBackend:
    """Fake duplex backend with settable input uid and output route."""

    def __init__(self) -> None:
        self.format = _INPUT_FORMAT
        self.device_uid = _INPUT_PROFILE.device_uid
        self.output_route: OutputRoute | None = _SPEAKERS
        self.output_script: list[OutputRoute | None] = []
        self.output_queries = 0
        # When set to the ingress control lock, every output query proves from
        # a helper thread that the caller is not holding it.
        self.lock_probe: threading.RLock | None = None
        self.lock_free_observations: list[bool] = []
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.version = 0
        # When set, the next open behaves like a foreign open that outlives
        # its bound: the device is claimed, but the typed result is uncertain.
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
        del frame_sink, render_source, timeout_s
        assert self.active_epoch is None
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.version += 1
        status, self.next_start_status = self.next_start_status, None
        if status is not None:
            return voice_backend.BackendStartResult(
                status=status,
                stream_epoch=stream_epoch,
                profile=None,
                reason="injected_open_timeout",
                attempt_id=attempt_id,
            )
        return voice_backend.BackendStartResult(
            status=voice_backend.BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=replace(
                _INPUT_PROFILE,
                device_uid=self.device_uid,
                device_name=self.device_uid,
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

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        del stream_epoch
        return None

    def current_device_uid(self) -> str | None:
        return self.device_uid

    def current_output_route(self) -> OutputRoute | None:
        self.output_queries += 1
        if self.lock_probe is not None:
            lock = self.lock_probe

            def _probe() -> None:
                acquired = lock.acquire(blocking=False)
                self.lock_free_observations.append(acquired)
                if acquired:
                    lock.release()

            probe = threading.Thread(target=_probe)
            probe.start()
            probe.join()
        if self.output_script:
            return self.output_script.pop(0)
        return self.output_route

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
        return voice_backend.BackendOwnershipSnapshot(
            state=(
                voice_backend.BackendLifecycleState.CLOSED
                if self.active_epoch is None
                else voice_backend.BackendLifecycleState.OPEN
            ),
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.version,
            physical_owner_possible=self.active_epoch is not None,
            helper_thread_alive=False,
        )


def _ingress(backend: _RouteBackend, *, observer: bool) -> voice_audio.AudioIngress:
    return voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=0.01,
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.002,
            shutdown_timeout_s=1.0,
            route_observer_enabled=observer,
        ),
    )


def _traces(name: str) -> list[dict[str, object]]:
    return [dict(point.attributes) for point in realtime_trace_snapshot() if point.name == name]


@contextlib.contextmanager
def _started(backend: _RouteBackend, *, observer: bool) -> Iterator[voice_audio.AudioIngress]:
    """Always close the ingress so a failed assertion cannot leak its worker."""
    reset_realtime_trace()
    ingress = _ingress(backend, observer=observer)
    assert ingress.start().started
    try:
        yield ingress
    finally:
        assert ingress.close().definitively_closed


def test_profile_resolves_at_open_and_re_resolves_on_output_only_change() -> None:
    """An output-only route change reopens the epoch through _handle_fault."""
    backend = _RouteBackend()
    with _started(backend, observer=True) as ingress:
        first = ingress.device_profile
        assert first is not None
        assert first.stream_epoch == 1
        assert first.allowed_barge_mode == "ptt"
        assert first.key == DeviceProfileKey(
            input_uid=_INPUT_PROFILE.device_uid,
            output_uid="BuiltInSpeakerDevice",
            backend="sounddevice",
            route_kind=RouteKind.SPEAKER,
            input_sample_rate=16_000,
            output_sample_rate=48_000,
        )
        assert ingress.capability.route_kind == "speaker"
        assert ingress.capability.allowed_barge_mode == "ptt"

        backend.output_route = replace(_SPEAKERS, uid="BlackHole16ch_UID", transport_type="virt")
        _wait_until(lambda: ingress.stream_epoch == 2)
        _wait_until(lambda: ingress.device_profile is not None)
        second = ingress.device_profile
        assert second is not None
        assert second.stream_epoch == 2
        assert second.key.input_uid == first.key.input_uid
        assert second.key.output_uid == "BlackHole16ch_UID"
        assert second.key.route_kind is RouteKind.UNKNOWN
        faults = [t["fault_code"] for t in _traces("audio_input_fault")]
        assert faults == ["output_route_changed"]
        changes = _traces("audio_route_changed")
        assert [c["previous_profile"] for c in changes] == [None, first.key.as_text()]
        assert changes[1]["next_profile"] == second.key.as_text()
        assert changes[1]["stream_epoch"] == 2
        assert changes[1]["reason"] == "recovery:output_route_changed"
        assert ingress.capability.route_kind == "unknown"
        assert ingress.close().definitively_closed


def test_input_change_re_resolves_through_the_existing_default_device_path() -> None:
    """The input poll still owns default_device_changed; the snapshot follows."""
    backend = _RouteBackend()
    with _started(backend, observer=True) as ingress:
        backend.device_uid = "sounddevice:0:allen Microphone"
        _wait_until(lambda: ingress.stream_epoch == 2)
        _wait_until(lambda: ingress.device_profile is not None)
        snapshot = ingress.device_profile
        assert snapshot is not None
        assert snapshot.stream_epoch == 2
        assert snapshot.key.input_uid == "sounddevice:0:allen Microphone"
        assert snapshot.key.output_uid == "BuiltInSpeakerDevice"
        assert [t["fault_code"] for t in _traces("audio_input_fault")] == ["default_device_changed"]
        assert len(_traces("audio_route_changed")) == 2
        assert ingress.close().definitively_closed


def test_observer_off_never_queries_output_and_keeps_input_path() -> None:
    """route_observer.enabled=false is today's input-only poll with ptt/unknown."""
    backend = _RouteBackend()
    with _started(backend, observer=False) as ingress:
        snapshot = ingress.device_profile
        assert snapshot is not None
        assert snapshot.key.output_uid is None
        assert snapshot.key.route_kind is RouteKind.UNKNOWN
        assert snapshot.allowed_barge_mode == "ptt"
        backend.output_route = replace(_SPEAKERS, uid="other")
        time.sleep(0.05)
        assert ingress.stream_epoch == 1
        backend.device_uid = "sounddevice:0:allen Microphone"
        _wait_until(lambda: ingress.stream_epoch == 2)
        assert backend.output_queries == 0
        assert _traces("audio_route_changed") == []
        assert ingress.close().definitively_closed


def test_transient_output_misses_are_tolerated_and_three_misses_fault_once() -> None:
    """A None query is a miss, not a change; three in a row re-resolve once."""
    backend = _RouteBackend()
    with _started(backend, observer=True) as ingress:
        queries = backend.output_queries
        backend.output_script = [None, None]
        _wait_until(lambda: backend.output_queries >= queries + 4)
        assert ingress.stream_epoch == 1
        assert _traces("audio_input_fault") == []
        backend.output_route = None
        _wait_until(lambda: ingress.stream_epoch == 2)
        _wait_until(lambda: ingress.device_profile is not None)
        profile = ingress.device_profile
        assert profile is not None
        assert profile.key.output_uid is None
        assert profile.key.route_kind is RouteKind.UNKNOWN
        time.sleep(0.05)
        assert ingress.stream_epoch == 2
        assert [t["fault_code"] for t in _traces("audio_input_fault")] == ["output_route_changed"]


def test_same_uid_data_source_flip_re_resolves_headphones() -> None:
    """D9 watches transport/data-source, not only the device uid."""
    backend = _RouteBackend()
    with _started(backend, observer=True) as ingress:
        backend.output_route = replace(_SPEAKERS, data_source="hdpn")
        _wait_until(lambda: ingress.stream_epoch == 2)
        _wait_until(lambda: ingress.device_profile is not None)
        profile = ingress.device_profile
        assert profile is not None
        assert profile.key.output_uid == _SPEAKERS.uid
        assert profile.key.route_kind is RouteKind.HEADPHONES
        assert profile.allowed_barge_mode == "ptt"
        assert [t["fault_code"] for t in _traces("audio_input_fault")] == ["output_route_changed"]


def test_sleep_revokes_the_profile_and_publishes_unknown_ptt() -> None:
    """The shared revocation point clears the snapshot for sleep, not only faults."""
    backend = _RouteBackend()
    reset_realtime_trace()
    ingress = _ingress(backend, observer=True)
    assert ingress.start().started
    assert ingress.capability.route_kind == "speaker"
    stopped = ingress.stop_for_sleep()
    assert stopped is not None
    assert stopped.definitively_closed
    assert ingress.device_profile is None
    assert ingress.capability.state is voice_audio.InputCapabilityState.SUSPENDED
    assert ingress.capability.route_kind == "unknown"
    assert ingress.capability.allowed_barge_mode == "ptt"
    assert ingress.close().definitively_closed
    assert ingress.capability.route_kind == "unknown"


def test_output_route_query_never_runs_under_the_control_lock() -> None:
    """The CoreAudio query is a foreign call; sleep/close must not wait on it."""
    backend = _RouteBackend()
    reset_realtime_trace()
    ingress = _ingress(backend, observer=True)
    backend.lock_probe = ingress._control_lock  # noqa: SLF001 - the pinned invariant
    assert ingress.start().started
    try:
        assert backend.lock_free_observations == [True]
        backend.output_route = replace(_SPEAKERS, uid="BlackHole16ch_UID", transport_type="virt")
        _wait_until(lambda: ingress.stream_epoch == 2)
        _wait_until(lambda: ingress.device_profile is not None)
        assert backend.lock_free_observations
        assert all(backend.lock_free_observations)
    finally:
        assert ingress.close().definitively_closed


def test_voice_capability_payload_carries_route_fields() -> None:
    """The v1 op gains two additive fields, also on the reconnect replay."""

    async def _scenario() -> None:
        broadcaster = InherentBroadcaster()
        await broadcaster.broadcast_voice_capability(
            version=1,
            state="available",
            stream_epoch=1,
            reason="input_stream_started",
            wake_available=True,
            local_capture_available=True,
            ptt_upload_available=True,
            text_available=True,
            route_kind="speaker",
            allowed_barge_mode="keyword_two_stage",
        )
        ws = MagicMock()
        ws.send_json = AsyncMock()
        await broadcaster.register(ws)
        payload = ws.send_json.await_args.args[0]["payload"]
        assert payload["route_kind"] == "speaker"
        assert payload["allowed_barge_mode"] == "keyword_two_stage"
        await broadcaster.broadcast_voice_capability(
            version=2,
            state="available",
            stream_epoch=2,
            reason="bounded_reopen_succeeded",
            wake_available=True,
            local_capture_available=True,
            ptt_upload_available=True,
            text_available=True,
        )
        payload = ws.send_json.await_args.args[0]["payload"]
        assert (payload["route_kind"], payload["allowed_barge_mode"]) == ("unknown", "ptt")

    asyncio.run(_scenario())


def test_file_replay_and_fake_backends_claim_no_aec(tmp_path: Path) -> None:
    """Regression pin across the non-production backends too."""
    wav_path = tmp_path / "silence.wav"
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(b"\x00" * 1024)
    for capabilities in (
        voice_backend.FileReplayBackend(wav_path).capabilities(),
        _RouteBackend().capabilities(),
    ):
        assert capabilities.aec is False
        assert capabilities.natural_barge_in is False
    assert voice_backend.FileReplayBackend(wav_path).current_output_route() is None


def test_late_settled_open_recovers_from_close_uncertain() -> None:
    """A reopen that times out but later settles CLOSED is retried (F17)."""
    backend = _RouteBackend()
    with _started(backend, observer=True) as ingress:
        backend.next_start_status = voice_backend.BackendStartStatus.OPEN_UNCERTAIN
        backend.output_route = replace(_SPEAKERS, uid="BlackHole16ch_UID", transport_type="virt")
        _wait_until(
            lambda: ingress.capability.state is voice_audio.InputCapabilityState.CLOSE_UNCERTAIN,
        )
        assert ingress.capability.reason == "reopen_state_uncertain"
        assert ingress.stream_epoch is None
        assert backend.active_epoch == 2
        # The foreign open settles late: the backend proves the device CLOSED.
        assert backend.active_attempt_id is not None
        backend.stop(stream_epoch=2, attempt_id=backend.active_attempt_id)
        _wait_until(
            lambda: ingress.capability.state is voice_audio.InputCapabilityState.AVAILABLE
            and ingress.stream_epoch == 3,
        )
        assert ingress.capability.reason == "late_close_recovered"
        profile = ingress.device_profile
        assert profile is not None
        assert profile.stream_epoch == 3
        assert profile.key.output_uid == "BlackHole16ch_UID"
        reopens = _traces("audio_input_reopen_succeeded")
        assert reopens[-1]["reason"] == "late_close_recovery"
