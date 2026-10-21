"""The audio-device and reSpeaker board watches: the Settings page's picks reach the hardware."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import time
from typing import TYPE_CHECKING, Any

from jarvis.runtime.daemon.voice_power import SUSPENDED, VoicePowerCoordinator
from jarvis.surface import respeaker_board, voice_audio, voice_backend, voice_live

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.runtime.settings import Settings

LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class AudioDeviceChoice:
    """The microphone and speaker to be on; the Settings page changes them live (ADR 0054)."""

    input_device: str | None
    output_device: str | None
    changed: bool = False

    def choose(self, input_device: str | None, output_device: str | None) -> None:
        """Record a pick made on the Settings page; the watch applies it on its next pass."""
        self.input_device, self.output_device, self.changed = input_device, output_device, True


AUDIO_DEVICE_POLL_S = 1.0
# ADR 0054: a microphone that stays shut while its device is there is tried again after these.
MIC_RETRY_S = (2.0, 5.0, 15.0, 30.0)


def output_or_default(name: str | None) -> str | None:
    """The picked speaker if CoreAudio lists it, else ``None``: the system default (ADR 0054)."""
    devices = voice_backend.coreaudio_devices("output") if isinstance(name, str) else None
    if devices is None or name in devices[1]:
        return name
    LOGGER.info("speaker %r is not there; playing on the system default", name)
    return None


def device_targets(choice: AudioDeviceChoice) -> tuple[int, int] | None:
    """The CoreAudio ids Jarvis should be on now: its pick if in, else the system default."""
    inputs = voice_backend.coreaudio_devices("input")
    outputs = voice_backend.coreaudio_devices("output")
    if inputs is None or outputs is None:
        return None
    return (
        inputs[1].get(choice.input_device, inputs[0]) if choice.input_device else inputs[0],
        outputs[1].get(choice.output_device, outputs[0]) if choice.output_device else outputs[0],
    )


async def watch_audio_devices(
    coordinator: VoicePowerCoordinator,
    ingress: voice_audio.AudioIngress,
    live_voice: voice_live.LiveVoice | None,
    choice: AudioDeviceChoice,
) -> None:
    """ADR 0054: re-read the devices when the ones to be on change, the mic is down, or on a pick.

    A pick that is absent counts as the system default, so the re-read opens
    that, and again on the pick when it is back.

    PortAudio lists only the devices there were when it initialised, so a
    microphone plugged back in stays invisible to every reopen until then.
    Other devices coming and going (a phone's microphone does, every minute or
    so) are not a reason: each re-read leaves the microphone deaf for a moment.
    A microphone that is down while its device is there (lost, or parked by a
    wake that failed) is tried again, sooner first, until it opens.
    """
    seen = await asyncio.to_thread(device_targets, choice)
    if seen is None:
        return
    retries, retry_at = 0, 0.0
    while True:
        await asyncio.sleep(AUDIO_DEVICE_POLL_S)
        now = await asyncio.to_thread(device_targets, choice)
        state = ingress.capability.state
        down = state is voice_audio.InputCapabilityState.LOCAL_CAPTURE_UNAVAILABLE or (
            state is SUSPENDED and not coordinator.asleep
        )
        if not down:
            retries, retry_at = 0, 0.0
        retry = down and now is not None and time.monotonic() >= retry_at
        if now is None or not (now != seen or retry or choice.changed) or not coordinator.quiet():
            continue
        picked = (choice.input_device, choice.output_device)
        wanted = (picked[0], await asyncio.to_thread(output_or_default, picked[1]))
        paused = (
            live_voice.output_paused(wanted[1])
            if live_voice is not None
            else contextlib.nullcontext(enter_result=True)
        )
        async with paused as free:
            if not free:
                continue
            outcome = await asyncio.to_thread(
                functools.partial(
                    coordinator.refresh_devices, input_device=wanted[0], output_device=wanted[1],
                ),
            )
        LOGGER.log(
            logging.DEBUG if outcome.startswith("busy") else logging.INFO,
            "audio devices re-read: %s (asked microphone=%s speaker=%s)",
            outcome, wanted[0] or "default", wanted[1] or "default",
        )
        if outcome.startswith("refreshed"):
            seen = now
            if (choice.input_device, choice.output_device) == picked:
                choice.changed = False
        if not outcome.startswith("busy"):
            retry_at = time.monotonic() + MIC_RETRY_S[min(retries, len(MIC_RETRY_S) - 1)]
            retries += 1


# ADR 0147: how often the reSpeaker's saved lights and levels are checked; a replug, a reboot
# and the board's boot animation (rainbow, then direction after 2 s) put its factory values back.
BOARD_POLL_S = 2.0


def _put_board(look: dict[str, Any]) -> str:
    """Put a saved board look on the reSpeaker if it is not there; one log line when written."""
    outcome = respeaker_board.ensure(look)
    if outcome == "applied":
        LOGGER.info("reSpeaker board: lights %s at %.0f%% put back on the board",
                    look["light"], 100 * look["brightness"])
    return outcome


async def watch_board(settings: Settings) -> None:
    """Keep the Settings page's board look on the reSpeaker (ADR 0147); no look saved, no writes."""
    settings.on_board = _put_board
    while True:
        look = await asyncio.to_thread(settings.board)
        if look is not None:
            await asyncio.to_thread(_put_board, look)
        await asyncio.sleep(BOARD_POLL_S)


def start_audio_device_watch(
    config: Mapping[str, Any],
    settings: Settings | None,
    coordinator: VoicePowerCoordinator,
    ingress: voice_audio.AudioIngress,
    live_voice: voice_live.LiveVoice | None,
) -> asyncio.Task[None]:
    """Start the ADR 0054 device watch and let the Settings page's device picks reach it."""
    realtime = config.get("realtime") or {}
    choice = AudioDeviceChoice(realtime.get("input_device"), realtime.get("output_device"))
    if settings is not None:
        settings.on_devices = choice.choose
    return asyncio.create_task(
        watch_audio_devices(coordinator, ingress, live_voice, choice),
        name="audio_device_watch",
    )
