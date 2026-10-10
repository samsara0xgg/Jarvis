"""ADR 0155 — which default output counts as private, from fake ``system_profiler`` answers.

``current_output`` reads the profile through ``_probe``; each check feeds it a canned JSON profile
(shaped like the real answer on this Mac) and asserts the device and the private flag, and that a
failed probe is never private.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.runtime import audio_output

if TYPE_CHECKING:
    from collections.abc import Callable

BT = "coreaudio_device_type_bluetooth"
BUILTIN = "coreaudio_device_type_builtin"


def _profile(*items: dict[str, Any]) -> str:
    return json.dumps({"SPAudioDataType": [{"_items": list(items)}]})


def _default(
    name: str,
    transport: str,
    source: str = "spaudio_default",
    **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    return {
        "_name": name,
        "coreaudio_default_audio_output_device": "spaudio_yes",
        "coreaudio_device_output": 2,
        "coreaudio_device_transport": transport,
        "coreaudio_output_source": source,
        **extra,
    }


# A default input with the same name as the AirPods, and a speaker that is not the default.
NOISE = [
    {"_name": "Allen's AirPods Pro", "coreaudio_device_transport": BT},
    {
        "_name": "MacBook Pro Speakers",
        "coreaudio_device_output": 2,
        "coreaudio_device_transport": BUILTIN,
        "coreaudio_output_source": "MacBook Pro Speakers",
    },
]

CASES = [
    ("airpods", _default("Allen's AirPods Pro", BT), True),
    ("any bluetooth device", _default("JBL Flip", BT), True),
    ("speakers", _default("MacBook Pro Speakers", BUILTIN, "MacBook Pro Speakers"), False),
    ("jack", _default("MacBook Pro Speakers", BUILTIN, "Headphones"), True),
    ("jack, any case", _default("MacBook Pro Speakers", BUILTIN, "HEADPHONES"), True),
    ("hdmi display", _default("DELL U2720Q", "coreaudio_device_type_displayport"), False),
    ("usb speaker", _default("Desk Speaker", "coreaudio_device_type_usb"), False),
    ("usb headphones by name", _default("USB Headphones", "coreaudio_device_type_usb"), True),
    ("earbuds by name", _default("Cheap Earbuds", "coreaudio_device_type_usb"), True),
    ("multi-output", _default("Multi-Output Device", "coreaudio_device_type_unknown"), False),
    ("aggregate", _default("Aggregate Device", "coreaudio_device_type_virtual"), False),
]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], None]:
    """Set what ``system_profiler`` answers (text, or an exception to raise); no cache."""
    monkeypatch.setattr(audio_output.sys, "platform", "darwin")
    monkeypatch.setattr(audio_output, "_cache", None)
    monkeypatch.setattr(audio_output, "_aggregate_parts", lambda: None)  # not an aggregate

    def answer(value: Any) -> None:  # noqa: ANN401
        def fake() -> str:
            if isinstance(value, BaseException):
                raise value
            return str(value)

        monkeypatch.setattr(audio_output, "_probe", fake)
        monkeypatch.setattr(audio_output, "_cache", None)

    return answer


@pytest.mark.parametrize(("label", "item", "private"), CASES, ids=[c[0] for c in CASES])
def test_the_default_output_is_private_only_for_headphone_like_devices(
    probe: Callable[[Any], None],
    label: str,  # noqa: ARG001
    item: dict[str, Any],
    private: bool,  # noqa: FBT001
) -> None:
    """The device the profile marks as default output, and whether it counts as private."""
    probe(_profile(*NOISE, item))
    out = audio_output.current_output()
    assert out["private"] is private
    assert out["name"] == item["_name"]
    assert out["transport"] == item["coreaudio_device_transport"]


@pytest.mark.parametrize(
    "answer",
    [
        subprocess.TimeoutExpired("system_profiler", 3),
        subprocess.CalledProcessError(1, "system_profiler"),
        FileNotFoundError("system_profiler"),
        "not json",
        "",
        "{}",
        json.dumps({"SPAudioDataType": None}),
        json.dumps({"SPAudioDataType": [{"_items": [{"_name": "No default"}]}]}),
        json.dumps({"SPAudioDataType": ["junk", 3]}),
        _profile(*NOISE),  # nothing is the default output
    ],
    ids=[
        "timeout",
        "exit 1",
        "no tool",
        "not json",
        "empty",
        "empty object",
        "null",
        "no default",
        "junk items",
        "no default device",
    ],
)
def test_a_failed_or_unreadable_probe_is_not_private(
    probe: Callable[[Any], None],
    answer: Any,  # noqa: ANN401
) -> None:
    """Any probe failure or odd shape fails closed and never raises."""
    probe(answer)
    assert audio_output.current_output()["private"] is False


def test_off_the_mac_is_not_private(
    probe: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No other OS has a probe; it is never private."""
    probe(_profile(_default("Allen's AirPods Pro", BT)))
    monkeypatch.setattr(audio_output.sys, "platform", "linux")
    assert audio_output.current_output()["private"] is False


def test_the_answer_is_cached_for_two_seconds_unless_fresh(
    probe: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A look is reused for two seconds; ``fresh`` always looks again."""
    probe(_profile(_default("Allen's AirPods Pro", BT)))
    assert audio_output.current_output()["private"] is True
    probe(_profile(_default("MacBook Pro Speakers", BUILTIN, "MacBook Pro Speakers")))
    # probe() clears the cache; take a reading, change the world, read again.
    assert audio_output.current_output()["private"] is False
    probe(_profile(_default("Allen's AirPods Pro", BT)))
    assert audio_output.current_output()["private"] is True
    monkeypatch.setattr(audio_output, "_probe", lambda: _profile(_default("X", BUILTIN, "X")))
    assert audio_output.current_output()["private"] is True  # still the cached look
    assert audio_output.current_output(fresh=True)["private"] is False
    assert audio_output.current_output()["private"] is False  # a fresh look refreshes the cache
    monkeypatch.setattr(audio_output.time, "monotonic", lambda: 1e9)
    monkeypatch.setattr(audio_output, "_probe", lambda: _profile(_default("A", BT)))
    assert audio_output.current_output()["private"] is True  # the cache expired


AIRPODS = ("Allen's AirPods Pro", "blue", False)
SPEAKERS = ("MacBook Pro Speakers", "bltn", False)
JACK = ("MacBook Pro Speakers", "bltn", True)
BLACKHOLE = ("BlackHole 2ch", "virt", False)
RESPEAKER = ("reSpeaker XVF3800 4-Mic Array", "usb ", False)

AGGREGATES = [
    ("airpods + blackhole", [AIRPODS, BLACKHOLE], True),
    ("airpods alone", [AIRPODS], True),
    ("jack + blackhole", [JACK, BLACKHOLE], True),
    ("headphone-like name, usb", [("USB Headphones", "usb ", False), BLACKHOLE], True),
    ("speakers + blackhole", [SPEAKERS, BLACKHOLE], False),
    ("airpods + speakers", [AIRPODS, SPEAKERS], False),
    ("speakers + reSpeaker (this Mac)", [SPEAKERS, RESPEAKER], False),
    ("airpods + unknown transport", [AIRPODS, ("Mystery", "\x00\x00\x00\x00", False)], False),
    ("only virtual parts", [BLACKHOLE], False),
    ("sub-devices unreadable", [], False),
]


@pytest.mark.parametrize(("label", "parts", "private"), AGGREGATES, ids=[c[0] for c in AGGREGATES])
def test_an_aggregate_output_is_private_only_when_its_real_parts_are(
    probe: Callable[[Any], None],
    monkeypatch: pytest.MonkeyPatch,
    label: str,  # noqa: ARG001
    parts: list[tuple[str, str, bool]],
    private: bool,  # noqa: FBT001
) -> None:
    """Virtual parts are ignored; every remaining part must be private, and at least one exists."""
    probe(_profile(_default("Multi-Output Device 2", "coreaudio_device_type_unknown")))
    monkeypatch.setattr(audio_output, "_aggregate_parts", lambda: parts)
    out = audio_output.current_output()
    assert out["private"] is private
    assert out["name"] == "Multi-Output Device 2"


def test_the_expanded_parts_are_logged_only_when_they_change(
    probe: Callable[[Any], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One INFO line per distinct sub-device list, naming each part and its transport."""
    probe(_profile(_default("Multi-Output Device 2", "coreaudio_device_type_unknown")))
    monkeypatch.setattr(audio_output, "_last_parts", None)
    parts = [SPEAKERS, RESPEAKER]
    monkeypatch.setattr(audio_output, "_aggregate_parts", lambda: parts)
    with caplog.at_level("INFO", logger=audio_output.LOGGER.name):
        audio_output.current_output(fresh=True)
        audio_output.current_output(fresh=True)
        assert len(caplog.records) == 1
        assert "MacBook Pro Speakers (bltn)" in caplog.text
        assert "reSpeaker XVF3800 4-Mic Array (usb)" in caplog.text
        parts = [AIRPODS]
        monkeypatch.setattr(audio_output, "_aggregate_parts", lambda: parts)
        audio_output.current_output(fresh=True)
    assert len(caplog.records) == 2


def test_a_non_aggregate_default_ignores_the_part_rule(
    probe: Callable[[Any], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` from the sub-device reader leaves the profile's own verdict alone."""
    probe(_profile(_default("Allen's AirPods Pro", BT)))
    monkeypatch.setattr(audio_output, "_aggregate_parts", lambda: None)
    assert audio_output.current_output()["private"] is True
