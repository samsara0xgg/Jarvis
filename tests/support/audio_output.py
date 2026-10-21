"""macOS audio-output profiles as the probe reads them, and the transports they name."""

from __future__ import annotations

import json
from typing import Any

BT = "coreaudio_device_type_bluetooth"


BUILTIN = "coreaudio_device_type_builtin"


def audio_profile(*items: dict[str, Any]) -> str:
    return json.dumps({"SPAudioDataType": [{"_items": list(items)}]})


def default_output(
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
