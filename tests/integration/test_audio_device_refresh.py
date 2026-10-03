"""ADR 0054: Jarvis re-reads the audio devices without dropping a word.

Four seams, each on the code it names. The coordinator closes every stream
before PortAudio initialises again and reopens on the chosen devices, and
closes nothing while an answer plays or Allen is mid-sentence. The media lane
parks an answer that arrives during the refresh and plays it after. The watch
re-reads only on a real change: the system's devices, a lost microphone, or a
pick on the Settings page. GPT-Live's speaker closes around the refresh,
never while it talks.
"""

# ruff: noqa: SLF001 — the composition root's private coordinator and watch are under test.
from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jarvis.runtime import inherent_loop
from jarvis.runtime.settings import Settings, apply_settings
from jarvis.state.event_log import open_event_log
from jarvis.surface import (
    voice_audio,
    voice_backend,
    voice_live,
    voice_media,
    voice_session,
    voice_tts,
)
from tests.integration.test_answer_waits_for_allen import _answer, _played
from tests.integration.test_settings import DEVICES, YAML
from tests.integration.test_wave2_streaming_media import (
    _Behavior,
    _CallbackPump,
    _config,
    _FakeProvider,
    _player,
    _submit_response,
)
from tests.integration.test_wave3_single_audio_ingress import _wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

_LOST = voice_audio.InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE
_PARKED = voice_audio.InputCapabilityState.SUSPENDED


def _capability(
    state: voice_audio.InputCapabilityState, version: int,
) -> voice_audio.InputCapabilitySnapshot:
    return voice_audio.InputCapabilitySnapshot(
        state=state, version=version, stream_epoch=None, reason="test",
        wake_available=False, local_capture_available=False,
    )


# --- coordinator ---------------------------------------------------------------


def _coordinator(
    actions: list[str],
    monkeypatch: pytest.MonkeyPatch,
    *,
    nothing_playing: bool = True,
    allen_talking: bool = False,
    speaker_closes: bool = True,
) -> inherent_loop._VoicePowerCoordinator:
    session = MagicMock(spec=voice_session.DuplexVoiceSession)
    ingress = session.ingress
    ingress.capability = _capability(_LOST, 3)
    ingress.capture_active = allen_talking

    def _stop_input(**_kwargs: object) -> voice_backend.BackendStopResult:
        actions.append("microphone closed")
        return voice_backend.BackendStopResult(voice_backend.BackendStopStatus.ALREADY_CLOSED, 1)

    def _start_input(**_kwargs: object) -> voice_backend.BackendStartResult:
        actions.append("microphone opened")
        return voice_backend.BackendStartResult(voice_backend.BackendStartStatus.STARTED, 2, None)

    ingress.stop_for_sleep.side_effect = _stop_input
    ingress.resume_after_wake.side_effect = _start_input
    ingress.set_input_device.side_effect = lambda device: actions.append(f"microphone={device}")
    def _hold(*, held: bool) -> bool:
        actions.append("hold" if held else "release")
        return nothing_playing

    def _stop_output() -> voice_tts.PlayerStopResult:
        actions.append("speaker closed")
        return voice_tts.PlayerStopResult(
            "closed" if speaker_closes else "uncertain", 1, "close_returned",
        )

    def _start_output() -> voice_tts.PlayerStartResult:
        actions.append("speaker opened")
        return voice_tts.PlayerStartResult("started", 2, "stream_started")

    media = MagicMock()
    media.hold_for_devices.side_effect = _hold
    player = media.player
    player.stop.side_effect = _stop_output
    player.set_device.side_effect = lambda device: actions.append(f"speaker={device}")
    player.start.side_effect = _start_output
    monkeypatch.setattr(
        voice_backend, "reinitialize_portaudio", lambda: actions.append("devices re-read"),
    )
    return inherent_loop._VoicePowerCoordinator(session=session, media=media)


def test_every_stream_closes_before_the_devices_are_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held answers, both streams closed, PortAudio anew, reopened on the picks, released."""
    actions: list[str] = []
    coordinator = _coordinator(actions, monkeypatch)
    outcome = coordinator.refresh_devices(input_device="reSpeaker", output_device="Speakers")
    assert actions == [
        "hold", "microphone closed", "speaker closed", "devices re-read",
        "speaker=Speakers", "speaker opened", "microphone=reSpeaker", "microphone opened",
        "release",
    ]
    assert outcome == "refreshed speaker=started:stream_started microphone=started"


@pytest.mark.parametrize(
    ("setup", "outcome", "expected"),
    [
        ({"nothing_playing": False}, "busy:speaking", ["hold", "release"]),
        ({"allen_talking": True}, "busy:listening", ["hold", "release"]),
        (
            {"speaker_closes": False},
            "not_refreshed:close_uncertain speaker=started:stream_started microphone=started",
            [
                "hold", "microphone closed", "speaker closed", "speaker=None",
                "speaker opened", "microphone=None", "microphone opened", "release",
            ],
        ),
    ],
)
def test_nothing_closes_while_she_speaks_or_he_talks(
    monkeypatch: pytest.MonkeyPatch,
    setup: dict[str, bool],
    outcome: str,
    expected: list[str],
) -> None:
    """Speaking or listening leaves every stream alone; a doubtful close never re-reads."""
    actions: list[str] = []
    coordinator = _coordinator(actions, monkeypatch, **setup)
    assert coordinator.refresh_devices(input_device=None, output_device=None) == outcome
    assert actions == expected


def test_a_wake_that_left_the_microphone_parked_re_reads_and_wakes_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Awake but parked: speaker closed, PortAudio anew, devices set, then the wake runs again."""
    actions: list[str] = []
    coordinator = _coordinator(actions, monkeypatch)
    coordinator._session.ingress.capability = _capability(_PARKED, 4)
    woke = inherent_loop._VoicePowerTransition(
        voice_backend.BackendStartResult(voice_backend.BackendStartStatus.STARTED, 5, None),
        voice_media.MediaPowerTransitionResult("resumed", 2, "fresh"),
        0.0,
        0.0,
    )
    monkeypatch.setattr(coordinator, "_wake", lambda: actions.append("woke") or woke)
    outcome = coordinator.refresh_devices(input_device="reSpeaker", output_device="Speakers")
    assert actions == [
        "speaker closed", "devices re-read", "speaker=Speakers", "microphone=reSpeaker", "woke",
    ]
    assert outcome == "refreshed_and_woke speaker=resumed:fresh microphone=started"


def test_nothing_is_touched_while_the_mac_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """From the sleep notice until a wake has run, the watch is not quiet and a refresh skips."""
    actions: list[str] = []
    coordinator = _coordinator(actions, monkeypatch)
    coordinator._session.ingress.capability = _capability(_PARKED, 4)
    coordinator._media.is_output_active.return_value = False
    coordinator.before_sleep()
    actions.clear()
    assert coordinator.asleep
    assert not coordinator.quiet()
    assert coordinator.refresh_devices(input_device=None, output_device=None) == "skipped:asleep"
    assert actions == []

    def _wake_into_a_new_sleep() -> object:
        coordinator.before_sleep()  # the lid closes again while the wake runs
        return None

    monkeypatch.setattr(coordinator, "_wake", _wake_into_a_new_sleep)
    coordinator.on_wake()
    assert coordinator.asleep
    monkeypatch.setattr(coordinator, "_wake", lambda: None)
    coordinator.on_wake()
    assert not coordinator.asleep
    assert coordinator.quiet()


# --- media lane ------------------------------------------------------------------


def test_an_answer_arriving_during_the_refresh_plays_after_it(tmp_path: Path) -> None:
    """Parked while held, still parked when Allen's own hold ends, played on release."""
    db_path = tmp_path / "hold.db"
    provider = _FakeProvider(
        {
            # A second of audio, so the first answer is still playing when asked.
            ("R-first", 0): _Behavior("success", final_delay_s=0.3, samples=8_000),
            ("R-late", 0): _Behavior("success"),
        },
        candidate_count=1,
    )
    player = _player(ring_seconds=2.0)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        start_player=False,
    )
    conn = open_event_log(db_path)
    try:
        with _CallbackPump(player):
            asyncio.run(_submit_response(pipeline, _answer(conn, "R-first", "T-first")))
            _wait_until(lambda: bool(provider.opened))
            # Something is playing: the refresh must not close the player.
            assert pipeline.hold_for_devices(held=True) is False
            pipeline.hold_for_devices(held=False)
            _wait_until(lambda: _played(tmp_path / "hold.db") == ["R-first"], timeout_s=5.0)
            _wait_until(lambda: not pipeline.is_output_active())
            assert pipeline.hold_for_devices(held=True) is True
            asyncio.run(_submit_response(pipeline, _answer(conn, "R-late", "T-late")))
            pipeline.hold_output(held=True)
            pipeline.hold_output(held=False)
            time.sleep(0.3)
            assert [rid for rid, _ in provider.opened] == ["R-first"]
            pipeline.hold_for_devices(held=False)
            _wait_until(
                lambda: _played(tmp_path / "hold.db") == ["R-first", "R-late"], timeout_s=5.0,
            )
    finally:
        assert pipeline.close()
        conn.close()


# --- the watch -------------------------------------------------------------------


def _system(
    monkeypatch: pytest.MonkeyPatch, inputs: dict[str, int], outputs: dict[str, int],
) -> dict[str, int]:
    """A fake CoreAudio: the named devices, and the defaults kept under ``in`` / ``out``."""
    defaults = {"in": next(iter(inputs.values())), "out": next(iter(outputs.values()))}
    monkeypatch.setattr(
        voice_backend,
        "coreaudio_devices",
        lambda kind: (
            (defaults["in"], dict(inputs)) if kind == "input" else (defaults["out"], dict(outputs))
        ),
    )
    monkeypatch.setattr(inherent_loop, "_AUDIO_DEVICE_POLL_S", 0.01)
    return defaults


def test_the_watch_re_reads_only_when_the_device_to_be_on_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone's mic coming and going: nothing. Busy: waits. A default, pick, or loss: once each."""
    inputs = {"reSpeaker": 142, "MacBook Pro Microphone": 86, "allen Microphone": 158}
    outputs = {"Multi-Output Device 2": 63, "Speakers": 55}
    defaults = _system(monkeypatch, inputs, outputs)
    ingress = MagicMock()
    ingress.capability = _capability(voice_audio.InputCapabilityState.AVAILABLE, 1)
    quiet = {"now": True}
    refreshes: list[tuple[str | None, str | None]] = []

    def _refresh(*, input_device: str | None, output_device: str | None) -> str:
        refreshes.append((input_device, output_device))
        # An absent pick opens the default, so the open never fails on it.
        ingress.capability = _capability(
            voice_audio.InputCapabilityState.AVAILABLE, ingress.capability.version + 1,
        )
        return "refreshed speaker=started microphone=started"

    coordinator = MagicMock(spec=inherent_loop._VoicePowerCoordinator)
    coordinator.quiet.side_effect = lambda: quiet["now"]
    coordinator.refresh_devices.side_effect = _refresh
    choice = inherent_loop._AudioDeviceChoice(None, "Speakers")

    async def _scenario() -> None:
        watch = asyncio.create_task(
            inherent_loop._watch_audio_devices(coordinator, ingress, None, choice),
        )
        await asyncio.sleep(0.1)
        del inputs["allen Microphone"]  # the phone's mic leaves and comes back
        await asyncio.sleep(0.1)
        inputs["allen Microphone"] = 159
        await asyncio.sleep(0.1)
        assert refreshes == []
        quiet["now"] = False
        del inputs["reSpeaker"]  # unplugged: the Mac's own mic becomes the default
        defaults["in"] = 86
        await asyncio.sleep(0.1)
        assert refreshes == []
        quiet["now"] = True
        await asyncio.sleep(0.1)
        assert refreshes == [(None, "Speakers")]
        choice.choose("reSpeaker", "Speakers")  # picked while it is still out
        await asyncio.sleep(0.1)
        assert refreshes[1:] == [("reSpeaker", "Speakers")]
        # That refresh's own failed open is not a new loss.
        await asyncio.sleep(0.1)
        assert len(refreshes) == 2
        inputs["reSpeaker"] = 200  # plugged back in
        await asyncio.sleep(0.1)
        assert refreshes[2:] == [("reSpeaker", "Speakers")]
        ingress.capability = _capability(_LOST, 40)  # the stream dies on its own
        await asyncio.sleep(0.1)
        assert refreshes[3:] == [("reSpeaker", "Speakers")]
        await asyncio.sleep(0.1)
        assert len(refreshes) == 4
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(_scenario())


def test_an_absent_pick_follows_the_default_and_the_pick_coming_back_switches_to_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unplug: the default's id is the target and the speaker is None. Plug in: back on the pick."""
    inputs = {"reSpeaker": 142, "MacBook Pro Microphone": 86}
    outputs = {"Multi-Output Device 2": 63, "MacBook Pro Speakers": 55}
    defaults = _system(monkeypatch, inputs, outputs)
    choice = inherent_loop._AudioDeviceChoice("reSpeaker", "Multi-Output Device 2")
    assert inherent_loop._device_targets(choice) == (142, 63)
    assert inherent_loop._output_or_default("Multi-Output Device 2") == "Multi-Output Device 2"
    ingress = MagicMock()
    ingress.capability = _capability(voice_audio.InputCapabilityState.AVAILABLE, 1)
    coordinator = MagicMock(spec=inherent_loop._VoicePowerCoordinator)
    coordinator.quiet.return_value = True
    coordinator.refresh_devices.return_value = "refreshed speaker=started microphone=started"

    async def _scenario() -> None:
        watch = asyncio.create_task(
            inherent_loop._watch_audio_devices(coordinator, ingress, None, choice),
        )
        await asyncio.sleep(0.1)
        assert coordinator.refresh_devices.call_count == 0
        del inputs["reSpeaker"], outputs["Multi-Output Device 2"]  # unplugged
        defaults["in"], defaults["out"] = 86, 55
        assert inherent_loop._device_targets(choice) == (86, 55)
        await asyncio.sleep(0.1)
        # The mic's own name stays: the backend finds it absent and opens the default.
        assert coordinator.refresh_devices.call_args_list[0].kwargs == {
            "input_device": "reSpeaker", "output_device": None,
        }
        assert coordinator.refresh_devices.call_count == 1
        inputs["reSpeaker"], outputs["Multi-Output Device 2"] = 142, 63  # plugged back in
        await asyncio.sleep(0.1)
        assert coordinator.refresh_devices.call_count == 2
        assert coordinator.refresh_devices.call_args.kwargs == {
            "input_device": "reSpeaker", "output_device": "Multi-Output Device 2",
        }
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(_scenario())


def test_the_watch_keeps_trying_a_microphone_that_is_there_but_shut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed opens with the device present: tried again, later each time, until it opens.

    A microphone parked by a failed wake counts as down once awake; asleep it waits.
    """
    _system(monkeypatch, {"reSpeaker": 142}, {"Speakers": 55})
    monkeypatch.setattr(inherent_loop, "_MIC_RETRY_S", (0.05, 0.15))
    ingress = MagicMock()
    ingress.capability = _capability(_LOST, 1)
    opens_after = {"tries": 4}
    refreshes: list[float] = []

    def _refresh(**_kwargs: object) -> str:
        refreshes.append(time.monotonic())
        opened = len(refreshes) >= opens_after["tries"]
        ingress.capability = _capability(
            voice_audio.InputCapabilityState.AVAILABLE if opened else _LOST,
            ingress.capability.version + 1,
        )
        return "refreshed speaker=started microphone=" + ("started" if opened else "failed_closed")

    coordinator = MagicMock(spec=inherent_loop._VoicePowerCoordinator)
    coordinator.quiet.return_value = True
    coordinator.asleep = False
    coordinator.refresh_devices.side_effect = _refresh

    async def _scenario() -> None:
        watch = asyncio.create_task(
            inherent_loop._watch_audio_devices(
                coordinator, ingress, None, inherent_loop._AudioDeviceChoice(None, None),
            ),
        )
        await _until(lambda: len(refreshes) == 4)
        gaps = [b - a for a, b in itertools.pairwise(refreshes)]
        assert gaps[0] >= 0.05
        assert gaps[1] >= 0.15
        await asyncio.sleep(0.3)
        assert len(refreshes) == 4  # open: no more tries
        coordinator.asleep = True
        ingress.capability = _capability(_PARKED, 9)  # the Mac sleeps
        await asyncio.sleep(0.3)
        assert len(refreshes) == 4
        opens_after["tries"] = 5
        coordinator.asleep = False  # the wake ran but left it parked
        await _until(lambda: len(refreshes) == 5)
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(_scenario())


async def _until(predicate: Callable[[], bool], timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


def test_the_watch_waits_while_gpt_live_talks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live speaking yields no pause, so nothing is closed or re-read until it stops."""
    defaults = _system(monkeypatch, {"reSpeaker": 1}, {"Multi-Output Device 2": 1})
    ingress = MagicMock()
    ingress.capability = _capability(voice_audio.InputCapabilityState.AVAILABLE, 1)
    coordinator = MagicMock(spec=inherent_loop._VoicePowerCoordinator)
    coordinator.quiet.return_value = True
    coordinator.refresh_devices.return_value = "refreshed"
    talking = {"now": True}

    @contextlib.asynccontextmanager
    async def _paused(_device: object) -> AsyncIterator[bool]:
        yield not talking["now"]

    live = MagicMock(spec=voice_live.LiveVoice)
    live.output_paused.side_effect = _paused

    async def _scenario() -> None:
        watch = asyncio.create_task(
            inherent_loop._watch_audio_devices(
                coordinator, ingress, live, inherent_loop._AudioDeviceChoice(None, None),
            ),
        )
        await asyncio.sleep(0.05)
        defaults["in"] = 4
        await asyncio.sleep(0.1)
        assert coordinator.refresh_devices.call_count == 0
        talking["now"] = False
        await asyncio.sleep(0.1)
        assert coordinator.refresh_devices.call_count == 1
        watch.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch

    asyncio.run(_scenario())


# --- GPT-Live -----------------------------------------------------------------------


def test_gpt_live_speaker_closes_around_the_refresh_and_reopens_on_the_pick() -> None:
    """While Live talks nothing closes; otherwise closed inside, reopened on the new speaker."""
    live = voice_live.LiveVoice(
        config=voice_live.gpt_live_config_from_mapping({}),
        broadcaster=MagicMock(),
        ingress=lambda: None,
        mic_muted=lambda: False,
        speech_muted=lambda: False,
    )
    player = MagicMock(spec=voice_tts.AudioStreamPlayer)
    player.bytes_pending.return_value = 0
    player.stop.return_value = voice_tts.PlayerStopResult("closed", 1, "close_returned")
    player.start.return_value = voice_tts.PlayerStartResult("started", 2, "stream_started")
    run = voice_live._LiveRun(epoch=1, player=player, subscription=MagicMock(), ws=None)
    live._run = run

    async def _scenario() -> None:
        run.writer_busy = True
        async with live.output_paused("Speakers") as free:
            assert free is False
        assert player.stop.call_count == 0
        assert player.start.call_count == 0
        run.writer_busy = False
        async with live.output_paused("Speakers") as free:
            assert free is True
            assert player.stop.call_count == 1
            assert player.start.call_count == 0
        player.set_device.assert_called_once_with("Speakers")
        assert player.start.call_count == 1

    asyncio.run(_scenario())


# --- Settings page -------------------------------------------------------------------


def test_a_device_pick_applies_at_once_when_a_voice_chain_runs(tmp_path: Path) -> None:
    """The picks reach the voice chain and wait for no restart; other keys still do."""
    settings = Settings(tmp_path, apply_settings(YAML, tmp_path), DEVICES.__getitem__)
    picked: list[tuple[str | None, str | None]] = []
    settings.on_devices = lambda microphone, speaker: picked.append((microphone, speaker))
    body = settings.update({"input_device": "MacBook Pro Microphone"})
    assert body["restart_pending"] is False
    body = settings.update({"output_device": "MacBook Pro Speakers", "wake_threshold": 0.9})
    assert body["restart_pending"] is True
    body = settings.update({"input_device": "System default", "wake_threshold": 0.95})
    assert body["restart_pending"] is False
    assert picked == [
        ("MacBook Pro Microphone", None),
        ("MacBook Pro Microphone", "MacBook Pro Speakers"),
        (None, "MacBook Pro Speakers"),
    ]
