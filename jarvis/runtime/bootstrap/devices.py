"""What sits on this machine or on the paired terminals: audio, sound output, drift."""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime import audio_output
from jarvis.runtime.settings_owner import SettingsDrift, check_own

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.job_mail import JobMail
    from jarvis.runtime.reminders import Reminders
    from jarvis.surface.terminal_link import TerminalHub


LOGGER = logging.getLogger(__name__)


def _settings_drift(
    hub: TerminalHub | None, paths: RuntimePaths, config: Mapping[str, Any],
) -> SettingsDrift | None:
    """ADR 0227: a brain warns of device keys in its own files and hears its terminals' settings."""
    if hub is None:
        return None
    check_own("brain", paths.settings, paths.root)
    drift = SettingsDrift(paths.root, paths.settings, config)
    hub.on_settings = drift.on_report
    return drift


def _audio_devices(kind: str) -> list[str]:
    """The Settings page's speaker / microphone choices; none when PortAudio cannot list them."""
    try:
        from jarvis.surface.voice_backend import device_names  # noqa: PLC0415 — loaded on demand.

        return device_names(kind)
    except Exception as exc:  # noqa: BLE001 — a text-only checkout has no audio stack.
        LOGGER.warning("settings: no %s devices: %s: %s", kind, type(exc).__name__, exc)
        return []


def _default_audio_device(kind: str) -> str | None:
    """The name the Settings page shows beside "System default"; none if CoreAudio will not say."""
    try:
        from jarvis.surface.voice_backend import default_device_name  # noqa: PLC0415 — on demand.

        return default_device_name(kind)
    except Exception as exc:  # noqa: BLE001 — a text-only checkout has no audio stack.
        LOGGER.warning("settings: no default %s device: %s: %s", kind, type(exc).__name__, exc)
        return None


def _no_audio_devices(_kind: str) -> list[str]:
    """The brain has no speaker or microphone to list."""
    return []


def _no_default_audio_device(_kind: str) -> None:
    """The brain has no default speaker or microphone to name."""


def _sound_output(
    hub: TerminalHub | None, job_mail: JobMail | None, reminders: Reminders,
) -> Callable[..., dict[str, Any]]:
    """ADR 0156: where sound would come out now, given to everything that asks.

    One machine plays on its own output. A brain (ADR 0170) asks the terminal that plays what she
    says unprompted.
    """
    output = (
        audio_output.current_output if hub is None
        else audio_output.TerminalOutput(partial(hub.call_player, timeout_s=_OUTPUT_TIMEOUT_S))
    )
    reminders.output = output
    if job_mail is not None:
        job_mail.output = output
    return output


_OUTPUT_TIMEOUT_S: Final[float] = 5.0
"""How long a brain waits for its terminal to say where sound would come out: its probe takes
at most 3 s, and a slow answer is a no (ADR 0156)."""
