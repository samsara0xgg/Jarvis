"""Parse Mandarin smart-home voice commands into canonical Hue control actions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

LOGGER = logging.getLogger(__name__)

COLOR_XY_MAP: dict[str, tuple[float, float]] = {
    "red": (0.675, 0.322),
    "红色": (0.675, 0.322),
    "orange": (0.561, 0.404),
    "橙色": (0.561, 0.404),
    "yellow": (0.443, 0.515),
    "黄色": (0.443, 0.515),
    "green": (0.409, 0.518),
    "绿色": (0.409, 0.518),
    "cyan": (0.17, 0.34),
    "青色": (0.17, 0.34),
    "blue": (0.167, 0.04),
    "蓝色": (0.167, 0.04),
    "purple": (0.272, 0.109),
    "紫色": (0.272, 0.109),
    "pink": (0.382, 0.16),
    "粉色": (0.382, 0.16),
    "white": (0.313, 0.329),
    "白色": (0.313, 0.329),
    "暖白": (0.459, 0.41),
    "暖白色": (0.459, 0.41),
    "淡蓝": (0.19, 0.20),
    "淡蓝色": (0.19, 0.20),
    "浅蓝": (0.19, 0.20),
    "浅蓝色": (0.19, 0.20),
    "天蓝": (0.19, 0.20),
    "天蓝色": (0.19, 0.20),
    "淡黄": (0.39, 0.38),
    "淡黄色": (0.39, 0.38),
    "淡绿": (0.28, 0.45),
    "淡绿色": (0.28, 0.45),
    "淡紫": (0.3, 0.15),
    "淡紫色": (0.3, 0.15),
    "玫红": (0.45, 0.17),
    "玫红色": (0.45, 0.17),
    "洋红": (0.45, 0.17),
    "深蓝": (0.153, 0.048),
    "深蓝色": (0.153, 0.048),
    "深红": (0.7, 0.29),
    "深红色": (0.7, 0.29),
    "tiffany蓝": (0.173, 0.348),
    "tiffany": (0.173, 0.348),
    "珊瑚": (0.534, 0.360),
    "珊瑚色": (0.534, 0.360),
    "薰衣草": (0.310, 0.270),
    "薰衣草色": (0.310, 0.270),
    "金色": (0.482, 0.467),
    "琥珀": (0.526, 0.440),
    "琥珀色": (0.526, 0.440),
    "柠檬": (0.368, 0.538),
    "柠檬色": (0.368, 0.538),
    "湖蓝": (0.170, 0.340),
    "湖蓝色": (0.170, 0.340),
    "酒红": (0.593, 0.255),
    "酒红色": (0.593, 0.255),
    "米白": (0.380, 0.380),
    "米白色": (0.380, 0.380),
}

COLOR_TEMP_MAP: dict[str, str] = {
    "暖光": "warm",
    "暖白": "warm",
    "warm": "warm",
    "自然光": "neutral",
    "中性光": "neutral",
    "neutral": "neutral",
    "冷光": "cool",
    "冷白": "cool",
    "cool": "cool",
}

_COLOR_CANONICAL_BY_ALIAS: dict[str, str] = {
    "red": "red",
    "红": "red",
    "红色": "red",
    "orange": "orange",
    "橙": "orange",
    "橙色": "orange",
    "yellow": "yellow",
    "黄": "yellow",
    "黄色": "yellow",
    "green": "green",
    "绿": "green",
    "绿色": "green",
    "cyan": "cyan",
    "青": "cyan",
    "青色": "cyan",
    "blue": "blue",
    "蓝": "blue",
    "蓝色": "blue",
    "purple": "purple",
    "紫": "purple",
    "紫色": "purple",
    "pink": "pink",
    "粉": "pink",
    "粉色": "pink",
    "white": "white",
    "白": "white",
    "白色": "white",
}

_COLOR_TEMP_CANONICAL_BY_ALIAS: dict[str, str] = {
    "暖光": "warm",
    "暖白": "warm",
    "warm": "warm",
    "自然光": "neutral",
    "中性光": "neutral",
    "neutral": "neutral",
    "冷光": "cool",
    "冷白": "cool",
    "cool": "cool",
}


@dataclass(frozen=True)
class _AliasEntry:
    """Canonical alias metadata for a device-like target."""

    canonical: str
    alias: str
    alias_type: str


class CommandParser:
    """Parse home automation commands into structured action dictionaries.

    Examples:
        "打开卧室灯" -> {"device": "bedroom_light", "action": "turn_on"}
        "关掉客厅所有灯" -> {"device": "living_room_group", "action": "turn_off"}
        "把书房灯调到 50%" -> {
            "device": "study_light",
            "action": "set_brightness",
            "value": 50,
        }
        "把卧室灯调成暖光" -> {
            "device": "bedroom_light",
            "action": "set_color_temp",
            "value": "warm",
        }
        "客厅灯变成蓝色" -> {
            "device": "living_room_light",
            "action": "set_color",
            "value": "blue",
        }
        "切换到阅读模式" -> {
            "device": "scene",
            "action": "activate",
            "value": "阅读模式",
        }
        "晚安" -> {"action": "voice_shortcut", "value": "晚安"}
    """

    def __init__(self, config: dict) -> None:
        """Initialize the parser with device and scene aliases from config.

        Args:
            config: Parsed application configuration dictionary.
        """

        hue_config = config.get("hue", config)
        self.logger = LOGGER
        self._device_aliases = self._build_alias_entries(
            light_aliases=hue_config.get("light_aliases", {}),
            group_aliases=hue_config.get("group_aliases", {}),
        )
        self._scene_aliases = self._build_name_lookup(hue_config.get("scene_aliases", {}))
        self._voice_shortcuts = self._build_name_lookup(hue_config.get("voice_shortcuts", {}))
        self._color_aliases = self._sorted_aliases(_COLOR_CANONICAL_BY_ALIAS)
        self._color_temp_aliases = self._sorted_aliases(_COLOR_TEMP_CANONICAL_BY_ALIAS)

    def parse(self, text: str) -> dict[str, Any]:
        """Parse a natural-language command into a Hue-friendly action payload.

        Args:
            text: Raw spoken command text.

        Returns:
            A dictionary describing the parsed action, or an error payload when
            no supported command pattern is matched.
        """

        raw_text = text.strip()
        normalized_text = self._normalize_text(raw_text)
        if not normalized_text:
            return {"error": "无法理解指令", "raw_text": raw_text}

        shortcut_match = self._resolve_named_alias(normalized_text, self._voice_shortcuts)
        if shortcut_match is not None:
            return {"action": "voice_shortcut", "value": shortcut_match}

        scene_result = self._parse_scene(raw_text)
        if scene_result is not None:
            return scene_result

        brightness_result = self._parse_brightness(raw_text)
        if brightness_result is not None:
            return brightness_result

        color_temp_result = self._parse_color_temp(raw_text)
        if color_temp_result is not None:
            return color_temp_result

        color_result = self._parse_color(raw_text)
        if color_result is not None:
            return color_result

        power_result = self._parse_power(raw_text)
        if power_result is not None:
            return power_result

        return {"error": "无法理解指令", "raw_text": raw_text}

    def _parse_scene(self, text: str) -> dict[str, Any] | None:
        """Parse scene activation commands."""

        match = re.fullmatch(r"(?:请)?(?:切换到|切到|启动|开启|进入)(?P<scene>.+)", text.strip())
        if not match:
            return None

        scene_name = self._resolve_named_alias(
            self._normalize_text(match.group("scene")),
            self._scene_aliases,
        )
        if scene_name is None:
            return None
        return {"device": "scene", "action": "activate", "value": scene_name}

    def _parse_brightness(self, text: str) -> dict[str, Any] | None:
        """Parse brightness adjustment commands."""

        match = re.fullmatch(
            r"(?:请)?(?:把)?(?P<device>.+?)(?:亮度)?(?:调到|调成|调为|设为|设置为)\s*(?P<value>\d{1,3})\s*[%％]",
            text.strip(),
        )
        if not match:
            return None

        device = self._resolve_device(match.group("device"))
        if device is None:
            return None

        brightness = max(0, min(100, int(match.group("value"))))
        return {"device": device, "action": "set_brightness", "value": brightness}

    def _parse_color_temp(self, text: str) -> dict[str, Any] | None:
        """Parse color temperature commands such as warm or cool light."""

        match = re.fullmatch(
            rf"(?:请)?(?:把)?(?P<device>.+?)(?:调成|调为|设为|设置为|变成)\s*(?P<value>{self._join_alias_pattern(self._color_temp_aliases)})",
            text.strip(),
        )
        if not match:
            return None

        device = self._resolve_device(match.group("device"))
        if device is None:
            return None

        color_temp = _COLOR_TEMP_CANONICAL_BY_ALIAS.get(
            self._normalize_text(match.group("value"))
        )
        if color_temp is None:
            return None

        return {"device": device, "action": "set_color_temp", "value": color_temp}

    def _parse_color(self, text: str) -> dict[str, Any] | None:
        """Parse color change commands and normalize them to English names."""

        match = re.fullmatch(
            rf"(?:请)?(?:把)?(?P<device>.+?)(?:调成|调为|设为|设置为|变成)\s*(?P<value>{self._join_alias_pattern(self._color_aliases)})",
            text.strip(),
        )
        if not match:
            return None

        device = self._resolve_device(match.group("device"))
        if device is None:
            return None

        color = _COLOR_CANONICAL_BY_ALIAS.get(self._normalize_text(match.group("value")))
        if color is None:
            return None

        return {"device": device, "action": "set_color", "value": color}

    def _parse_power(self, text: str) -> dict[str, Any] | None:
        """Parse turn-on and turn-off commands."""

        stripped_text = text.strip()
        patterns = (
            (r"(?:请)?(?:打开|开启|开)(?P<device>.+)", "turn_on"),
            (r"(?:请)?(?:关掉|关闭|关上|关)(?P<device>.+)", "turn_off"),
            (r"(?:请)?(?:把)?(?P<device>.+?)(?:打开|开启|开)$", "turn_on"),
            (r"(?:请)?(?:把)?(?P<device>.+?)(?:关掉|关闭|关上|关)$", "turn_off"),
        )

        for pattern, action in patterns:
            match = re.fullmatch(pattern, stripped_text)
            if match is None:
                continue

            device = self._resolve_device(match.group("device"))
            if device is None:
                return None
            return {"device": device, "action": action}

        return None

    def _resolve_device(self, text: str) -> str | None:
        """Resolve a device alias using longest-match semantics."""

        normalized_text = self._normalize_text(text)
        for entry in self._device_aliases:
            if normalized_text == entry.alias:
                return entry.canonical
        for entry in self._device_aliases:
            if entry.alias in normalized_text:
                return entry.canonical
        self.logger.debug("No device alias matched: %s", text)
        return None

    def _build_alias_entries(
        self,
        light_aliases: dict[str, list[str] | str],
        group_aliases: dict[str, list[str] | str],
    ) -> list[_AliasEntry]:
        """Build a length-sorted alias table for devices and groups."""

        entries: list[_AliasEntry] = []
        for alias_type, alias_group in (
            ("light", light_aliases),
            ("group", group_aliases),
        ):
            for canonical, aliases in alias_group.items():
                for alias in self._coerce_aliases(aliases):
                    entries.append(
                        _AliasEntry(
                            canonical=canonical,
                            alias=self._normalize_text(alias),
                            alias_type=alias_type,
                        )
                    )
        return sorted(entries, key=lambda entry: len(entry.alias), reverse=True)

    def _build_name_lookup(
        self,
        alias_group: dict[str, list[str] | str],
    ) -> dict[str, str]:
        """Build an alias-to-canonical lookup for scenes and shortcuts."""

        lookup: dict[str, str] = {}
        for canonical, aliases in alias_group.items():
            lookup[self._normalize_text(canonical)] = canonical
            for alias in self._coerce_aliases(aliases):
                lookup[self._normalize_text(alias)] = canonical
        return lookup

    def _resolve_named_alias(
        self,
        normalized_text: str,
        lookup: dict[str, str],
    ) -> str | None:
        """Resolve direct alias matches for scenes and voice shortcuts."""

        if normalized_text in lookup:
            return lookup[normalized_text]

        for alias, canonical in sorted(lookup.items(), key=lambda item: len(item[0]), reverse=True):
            if normalized_text == alias:
                return canonical
        return None

    def _coerce_aliases(self, aliases: list[str] | str) -> list[str]:
        """Normalize alias config values into a flat string list."""

        if isinstance(aliases, str):
            return [aliases]
        return [alias for alias in aliases if alias]

    def _normalize_text(self, text: str) -> str:
        """Normalize text for regex-free comparisons and alias lookup."""

        return re.sub(r"\s+", "", text).lower()

    def _sorted_aliases(self, mapping: dict[str, str]) -> list[str]:
        """Return aliases sorted by length so regex matching prefers longer text."""

        return sorted(mapping.keys(), key=len, reverse=True)

    def _join_alias_pattern(self, aliases: list[str]) -> str:
        """Build a regex alternation pattern from a list of aliases."""

        return "|".join(re.escape(alias) for alias in aliases)
