"""ADR-0006 D9 device profile resolver and route observer acceptance."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from jarvis.surface import voice_audio, voice_backend
from jarvis.surface.voice_backend import DeviceProfileKey, OutputRoute, RouteKind

if TYPE_CHECKING:
    from collections.abc import Callable

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

