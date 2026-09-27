"""Jarvis's own settings (ADR 0052): a daemon-owned file laid over config/jarvis.yaml at boot.

The desktop Settings page reads and writes ``<runtime root>/settings.json``
through ``/inherent/settings``. Every value is read once at boot, so a saved
change waits for a restart; ``restart_pending`` says whether one does. The
microphone and speaker are the exception: a running voice chain takes them at
once (ADR 0054).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from typing import TYPE_CHECKING, Any

from jarvis.shared import lang
from jarvis.state.plugin_settings import write_private_json

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

LOGGER = logging.getLogger(__name__)
SETTINGS_FILE = "settings.json"
SYSTEM_DEFAULT = "System default"
_MANDARIN = "Chinese (Mandarin)_"
_ENGLISH = "English_"
# MiniMax's Mandarin system voices, from its get_voice answer of 2026-09-25.
VOICES = tuple(
    _MANDARIN + name
    for name in (
        "Warm_Bestie", "ExplorativeGirl", "Sweet_Lady", "Warm_Girl", "Crisp_Girl", "Soft_Girl",
        "IntellectualGirl", "Warm_HeartedGirl", "Laid_BackGirl", "BashfulGirl", "Cute_Spirit",
        "Mature_Woman", "News_Anchor", "Wise_Women", "HK_Flight_Attendant", "Kind-hearted_Antie",
        "Warm-HeartedAunt", "Gentle_Senior", "Kind-hearted_Elder", "Gentle_Youth", "Gentleman",
        "Reliable_Executive", "Unrestrained_Young_Man", "Southern_Young_Man", "Stubborn_Friend",
        "Straightforward_Boy", "Pure-hearted_Boy", "Sincere_Adult", "Lyrical_Voice",
        "Radio_Host", "Male_Announcer", "Humorous_Elder",
    )
) + tuple(
    # English ones first-run setup offers (same get_voice answer, 2026-09-26).
    _ENGLISH + name
    for name in ("radiant_girl", "CalmWoman", "FriendlyPerson", "Trustworth_Man")
)
# The five voices first-run setup offers per language; the first is the default
# when ``realtime.tts_voice`` is left empty.
SETUP_VOICES: dict[str, tuple[str, ...]] = {
    "zh": tuple(
        _MANDARIN + name
        for name in ("Warm_Bestie", "Sweet_Lady", "Mature_Woman", "Reliable_Executive",
                     "Gentle_Youth")
    ),
    # Warm Bestie leads here too: Allen picked it over every English voice (2026-09-26).
    "en": (_MANDARIN + "Warm_Bestie", *VOICES[-4:]),
}
# Each key the page may change and the config value it sets.
PATHS: dict[str, tuple[str, ...]] = {
    "reply_language": ("reply_language",),
    "wake_threshold": ("realtime", "wake_threshold"),
    "tts_voice": ("realtime", "tts_voice"),
    "tts_volume": ("realtime", "playback_volume"),
    "output_device": ("realtime", "output_device"),
    "input_device": ("realtime", "input_device"),
    "gpt_live": ("realtime", "gpt_live", "enabled"),
    "mac_aec": ("realtime", "single_audio_ingress", "echo_cancellation"),
    "timesink": ("observer", "timesink", "enabled"),
    "keep_audio": ("memory", "retain_audio"),
}
_DEFAULTS: dict[str, Any] = {"reply_language": "follow", "tts_volume": 1.0}
_DEVICES = {"output_device": "output", "input_device": "input"}
_RANGES = {"wake_threshold": (0.80, 0.99), "tts_volume": (0.3, 1.0)}
_SWITCHES = ("gpt_live", "mac_aec", "timesink", "keep_audio")
REPLY_LINES = {
    "zh": "Reply language: always answer in Chinese (Mandarin), whatever language the user uses.",
    "en": "Reply language: always answer in English, whatever language the user uses.",
}


def voice_name(voice_id: str) -> str:
    """What the page shows: first-run setup's name for the voice when it has one.

    ``English_radiant_girl`` -> ``Radiant Girl`` / ``明亮女孩`` (current language);
    any other ``Chinese (Mandarin)_Warm_HeartedGirl`` -> ``Warm Hearted Girl``.
    """
    if f"voice.{voice_id}" in lang.TEXT:
        return lang.t(f"voice.{voice_id}")
    name = voice_id.removeprefix(_MANDARIN).removeprefix(_ENGLISH).replace("_", " ")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def _valid(key: str, value: object) -> bool:
    """Whether ``value`` is a storable value for ``key``; devices are checked against the list."""
    if key in _RANGES:
        low, high = _RANGES[key]
        number = isinstance(value, int | float) and not isinstance(value, bool)
        return number and low <= value <= high  # type: ignore[operator]
    if key in _SWITCHES:
        return isinstance(value, bool)
    if key in _DEVICES:
        return value is None or isinstance(value, str)
    if key == "tts_voice":
        return value in VOICES
    return key == "reply_language" and value in ("follow", "zh", "en")


def _saved(root: Path) -> dict[str, Any]:
    """The file's valid entries; a missing or broken file means no settings, never a failed boot."""
    path = root / SETTINGS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOGGER.warning("settings: %s unreadable, ignored: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    kept = {key: value for key, value in raw.items() if key in PATHS and _valid(key, value)}
    for key in raw.keys() - kept.keys():
        LOGGER.warning("settings: ignored %s=%r in %s", key, raw[key], path)
    return kept


def _get(config: Mapping[str, Any], path: tuple[str, ...]) -> Any:  # noqa: ANN401 — a config value.
    node: Any = config
    for part in path:
        node = node.get(part) if isinstance(node, dict) else None
    return node


def apply_settings(config: Mapping[str, Any], root: Path) -> dict[str, Any]:
    """The config with the saved settings laid over it; the YAML dict itself is not changed."""
    merged = copy.deepcopy(dict(config))
    for key, value in _saved(root).items():
        *parents, leaf = PATHS[key]
        node = merged
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return merged


class Settings:
    """The page's view of the settings; this process is the file's only writer."""

    def __init__(
        self, root: Path, config: Mapping[str, Any], devices: Callable[[str], list[str]]
    ) -> None:
        """Bind the runtime root, the merged boot config and the audio device lister."""
        self._root = root
        self._config = config
        self._devices = devices
        # What this process runs with: the boot values, and a device picked
        # since when a voice chain took it live.
        self._booted = {
            key: _get(config, path) if _get(config, path) is not None else _DEFAULTS.get(key)
            for key, path in PATHS.items()
        }
        # ADR 0054: set by the voice chain; takes (microphone, speaker) at once.
        self.on_devices: Callable[[str | None, str | None], None] | None = None

    def read(self) -> dict[str, Any]:
        """``{values, options, restart_pending}`` in the shapes the Settings page shows."""
        saved = _saved(self._root)
        current = {**self._booted, **saved}
        values: dict[str, Any] = {key: self._shown(key, value) for key, value in current.items()}
        values["repos"] = list(_get(self._config, ("observer", "repos")) or [])
        report = _get(self._config, ("daily_report", "preset"))
        for key, preset in (
            ("model_conversation", _get(self._config, ("llm", "default_preset"))),
            ("model_background", _get(self._config, ("work_state", "preset"))),
            ("model_report", report or _get(self._config, ("work_state", "preset"))),
        ):
            model = _get(self._config, ("llm", "presets", str(preset), "model"))
            values[key] = str(model or preset or "")
        return {
            "values": values,
            "options": {
                "tts_voice": [voice_name(one) for one in VOICES],
                **{key: [SYSTEM_DEFAULT, *self._devices(kind)] for key, kind in _DEVICES.items()},
            },
            "restart_pending": any(value != self._booted[key] for key, value in saved.items()),
        }

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Save the page's changes, then answer like :meth:`read`; ValueError names a bad one."""
        stored = {key: self._stored(key, value) for key, value in changes.items()}
        write_private_json(self._root / SETTINGS_FILE, {**_saved(self._root), **stored})
        picked = stored.keys() & _DEVICES.keys()
        if picked and self.on_devices is not None:
            for key in picked:
                self._booted[key] = stored[key]
            self.on_devices(self._booted["input_device"], self._booted["output_device"])
        return self.read()

    def _shown(self, key: str, value: object) -> object:
        if key in _DEVICES:
            return value or SYSTEM_DEFAULT
        if key == "tts_voice" and isinstance(value, str):
            return voice_name(value)
        return value

    def _stored(self, key: str, value: object) -> object:
        if key not in PATHS:
            msg = f"{key} is not a setting the panel can change"
            raise ValueError(msg)
        if key in _DEVICES:
            if value == SYSTEM_DEFAULT:
                return None
            if value not in self._devices(_DEVICES[key]):
                msg = f"no {_DEVICES[key]} device named {value!r}"
                raise ValueError(msg)
        if key == "tts_voice":
            value = next((one for one in VOICES if voice_name(one) == value), value)
        if not _valid(key, value):
            msg = f"{key} cannot be {value!r}"
            raise ValueError(msg)
        return value
