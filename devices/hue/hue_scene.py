"""Scene helpers for listing and activating Philips Hue scenes."""

from __future__ import annotations

import logging
from typing import Any

from devices.base_device import SmartDevice
from devices.hue.hue_bridge import HueBridge

LOGGER = logging.getLogger(__name__)


class HueSceneDevice(SmartDevice):
    """Virtual smart device used to activate Hue scenes by name."""

    def __init__(
        self,
        bridge: HueBridge,
        scene_aliases: dict[str, list[str] | str],
        default_group_id: str | None = None,
        required_role: str = "guest",
    ) -> None:
        """Initialize the scene activation device."""

        super().__init__(
            device_id="scene",
            name="Hue Scenes",
            device_type="scene",
            required_role=required_role,
            is_available=True,
        )
        self.bridge = bridge
        self.scene_aliases = scene_aliases
        self.default_group_id = default_group_id
        self.logger = LOGGER

    def execute(self, action: str, value: Any | None = None) -> str:
        """Execute a scene activation request."""

        if action.strip().lower() != "activate":
            raise ValueError(f"Unsupported scene action: {action}")
        if value is None:
            raise ValueError("Scene activation requires a scene name.")
        activated = self.activate_scene(str(value), self.default_group_id)
        return f"已激活场景：{activated}。"

    def get_status(self) -> dict[str, Any]:
        """Return the currently available scenes."""

        return {
            "device_id": self.device_id,
            "name": self.name,
            "device_type": self.device_type,
            "required_role": self.required_role,
            "is_available": True,
            "scenes": self.list_scenes(),
        }

    def list_scenes(self) -> list[dict[str, Any]]:
        """Return all scenes from the bridge as a flat list."""

        scenes = self.bridge.get_all_scenes()
        results: list[dict[str, Any]] = []
        for scene_id, scene in scenes.items():
            results.append(
                {
                    "id": scene_id,
                    "name": scene.get("name"),
                    "group": scene.get("group"),
                    "lights": scene.get("lights", []),
                }
            )
        return results

    def activate_scene(self, name: str, group_id: str | None = None) -> str:
        """Fuzzy-match a scene name and activate it for the given group."""

        requested = name.strip()
        scene_id, scene = self._find_scene(requested)
        target_group_id = str(group_id or scene.get("group") or self.default_group_id or "").strip()
        if not target_group_id:
            raise ValueError(f"Scene '{requested}' does not have an associated group.")

        self.bridge.request(
            "PUT",
            f"/api/{self.bridge.username}/groups/{target_group_id}/action",
            {"scene": scene_id},
        )
        return str(scene.get("name", requested))

    def _find_scene(self, requested_name: str) -> tuple[str, dict[str, Any]]:
        """Find the best scene match by alias or fuzzy containment."""

        normalized_requested = self._normalize(requested_name)
        alias_candidates = {normalized_requested}
        for canonical, aliases in self.scene_aliases.items():
            normalized_canonical = self._normalize(canonical)
            normalized_aliases = {self._normalize(alias) for alias in self._coerce_aliases(aliases)}
            if normalized_requested == normalized_canonical or normalized_requested in normalized_aliases:
                alias_candidates.add(normalized_canonical)
                alias_candidates.update(normalized_aliases)

        scenes = self.bridge.get_all_scenes()
        best_match: tuple[str, dict[str, Any]] | None = None
        best_score = -1
        for scene_id, scene in scenes.items():
            scene_name = str(scene.get("name", ""))
            normalized_scene_name = self._normalize(scene_name)
            score = -1
            if normalized_scene_name in alias_candidates:
                score = 3
            elif normalized_requested == normalized_scene_name:
                score = 2
            elif normalized_requested in normalized_scene_name or normalized_scene_name in normalized_requested:
                score = 1
            if score > best_score:
                best_match = (scene_id, scene)
                best_score = score

        if best_match is None or best_score < 0:
            raise ValueError(f"Scene not found: {requested_name}")
        return best_match

    def _coerce_aliases(self, aliases: list[str] | str) -> list[str]:
        """Normalize config alias values into a list."""

        if isinstance(aliases, str):
            return [aliases]
        return [alias for alias in aliases if alias]

    def _normalize(self, text: str) -> str:
        """Normalize text for matching."""

        return "".join(text.lower().split())
