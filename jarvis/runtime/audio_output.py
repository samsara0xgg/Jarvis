"""Is the Mac's default sound output private (ADR 0155)? Unprompted audio plays only if it is.

Allen may be in public with headphones: nothing Jarvis says or plays without being asked may come
out of the built-in speakers or an unknown device. ``current_output`` asks ``system_profiler`` for
the default output and answers ``{name, transport, private}``. Only a Bluetooth device, the
built-in headphone jack, or a device named like headphones is private; everything else, and every
failure to find out, is not.

An aggregate or multi-output default is read as the devices it plays through: virtual parts (a
loopback such as BlackHole) are ignored, and it is private only if every real part is.

A brain (ADR 0170) has no sound output to ask: :class:`TerminalOutput` asks the terminal that plays,
which answers with its own ``current_output``, and the same rules stand.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import device_reads
from jarvis.state.daily_contract import DailyError

if TYPE_CHECKING:
    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)

_PROBE: Final[list[str]] = ["system_profiler", "SPAudioDataType", "-json"]
_TIMEOUT_S: Final[float] = 3.0
_CACHE_S: Final[float] = 2.0
_BLUETOOTH: Final[str] = "coreaudio_device_type_bluetooth"
_BUILTIN: Final[str] = "coreaudio_device_type_builtin"
_NAME: Final[re.Pattern[str]] = re.compile(
    r"headphone|airpods|buds|earphone|earbuds", re.IGNORECASE
)
_BLUETOOTH_FOURCC: Final[frozenset[str]] = frozenset({"blue", "blea"})
_NONE: Final[dict[str, Any]] = {"name": "", "transport": "", "private": False}

_cache: tuple[float, dict[str, Any]] | None = None
_last_parts: list[tuple[str, str, bool]] | None = None


def classify(profile: Any) -> dict[str, Any]:  # noqa: ANN401 - parsed JSON, any shape
    """The default output in a parsed ``SPAudioDataType`` profile; never raises."""
    try:
        for group in profile["SPAudioDataType"]:
            for item in group.get("_items") or []:
                if item.get("coreaudio_default_audio_output_device") == "spaudio_yes":
                    name = str(item.get("_name") or "")
                    transport = str(item.get("coreaudio_device_transport") or "")
                    source = str(item.get("coreaudio_output_source") or "")
                    private = (
                        transport == _BLUETOOTH
                        or (transport == _BUILTIN and "headphone" in source.lower())
                        or bool(_NAME.search(name))
                    )
                    return {"name": name, "transport": transport, "private": private}
    except (KeyError, TypeError, AttributeError):
        pass
    return dict(_NONE)


def _probe() -> str:
    return subprocess.run(  # noqa: S603 - a fixed command
        _PROBE, capture_output=True, text=True, timeout=_TIMEOUT_S, check=True
    ).stdout


def _aggregate_parts() -> list[tuple[str, str, bool]] | None:
    """``(name, transport, on the jack)`` of the default aggregate's sub-devices, else ``None``."""
    from jarvis.surface.voice_backend import aggregate_output_parts  # noqa: PLC0415 - darwin only

    return aggregate_output_parts()


def _parts_private(parts: list[tuple[str, str, bool]]) -> bool:
    """Some real (non-virtual) part exists and each is Bluetooth, on the jack or headphone-named."""
    global _last_parts  # noqa: PLW0603 - log only when the expansion changes
    if parts != _last_parts:
        _last_parts = list(parts)
        LOGGER.info(
            "audio output: aggregate default expands to %s",
            [f"{name} ({transport.strip()})" for name, transport, _ in parts] or "nothing readable",
        )
    real = [part for part in parts if part[1] != "virt"]
    return bool(real) and all(
        transport in _BLUETOOTH_FOURCC
        or (transport == "bltn" and jack)
        or bool(_NAME.search(name))
        for name, transport, jack in real
    )


def current_output(*, fresh: bool = False) -> dict[str, Any]:
    """``{name, transport, private}`` of the default output, cached for 2 s unless ``fresh``."""
    global _cache  # noqa: PLW0603 - one two-second cache for the whole daemon
    now = time.monotonic()
    if not fresh and _cache is not None and now - _cache[0] < _CACHE_S:
        return dict(_cache[1])
    if sys.platform != "darwin":
        found = dict(_NONE)
    else:
        try:
            found = classify(json.loads(_probe()))
        except (OSError, subprocess.SubprocessError, ValueError):
            found = dict(_NONE)
        parts = _aggregate_parts()
        if parts is not None:
            found["private"] = _parts_private(parts)
    _cache = (time.monotonic(), found)
    return dict(found)


class TerminalOutput:
    """A brain's ``current_output`` (ADR 0156, 0170): the default output of the terminal that plays.

    ``device`` is a link to that terminal. Each look asks it, kept for the same two seconds as a
    local one unless ``fresh``. A terminal that is away, slow, too old to know the read or that
    answers with anything but a clear yes is not private, and that no is kept as long as a yes.
    """

    def __init__(self, device: DeviceLink) -> None:
        """Bind the link to the terminal that plays what she says unprompted."""
        self._device = device
        self._lock = threading.Lock()
        self._kept: tuple[float, dict[str, Any]] | None = None

    def __call__(self, *, fresh: bool = False) -> dict[str, Any]:
        """``{name, transport, private}`` as the terminal reports it, else not private."""
        with self._lock:  # a poll and a decision share one ask
            if not fresh and self._kept is not None and time.monotonic() - self._kept[0] < _CACHE_S:
                return dict(self._kept[1])
            found = self._ask(fresh=fresh)
            self._kept = (time.monotonic(), found)
            return dict(found)

    def _ask(self, *, fresh: bool) -> dict[str, Any]:
        try:
            got = device_reads.ask(
                self._device, device_reads.TIMESINK_READ, "audio_output", fresh=fresh,
            )
        except DailyError as exc:
            LOGGER.warning("audio output: the terminal's read failed, so not private: %s", exc)
            return dict(_NONE)
        if not isinstance(got, dict):
            return dict(_NONE)
        return {
            "name": str(got.get("name") or ""),
            "transport": str(got.get("transport") or ""),
            "private": got.get("private") is True,
        }
