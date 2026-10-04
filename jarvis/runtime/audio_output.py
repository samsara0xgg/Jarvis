"""Is the Mac's default sound output private (ADR 0155)? Unprompted audio plays only if it is.

Allen may be in public with headphones: nothing Jarvis says or plays without being asked may come
out of the built-in speakers or an unknown device. ``current_output`` asks ``system_profiler`` for
the default output and answers ``{name, transport, private}``. Only a Bluetooth device, the
built-in headphone jack, or a device named like headphones is private; everything else, and every
failure to find out, is not.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any, Final

_PROBE: Final[list[str]] = ["system_profiler", "SPAudioDataType", "-json"]
_TIMEOUT_S: Final[float] = 3.0
_CACHE_S: Final[float] = 2.0
_BLUETOOTH: Final[str] = "coreaudio_device_type_bluetooth"
_BUILTIN: Final[str] = "coreaudio_device_type_builtin"
_NAME: Final[re.Pattern[str]] = re.compile(
    r"headphone|airpods|buds|earphone|earbuds", re.IGNORECASE
)
_NONE: Final[dict[str, Any]] = {"name": "", "transport": "", "private": False}

_cache: tuple[float, dict[str, Any]] | None = None


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
    _cache = (time.monotonic(), found)
    return dict(found)
