"""Scene automation engine — execute multi-step automation sequences."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class AutomationEngine:
    """Execute multi-step automation scenes."""

    def __init__(
        self,
        device_manager: Any,
        event_bus: Any = None,
        tts_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.device_manager = device_manager
        self.event_bus = event_bus
        self.tts_callback = tts_callback
        self._scenes: dict[str, list[dict[str, Any]]] = {}
        self.logger = LOGGER

    def register_scene(self, name: str, steps: list[dict[str, Any]]) -> None:
        """Register an automation scene."""
        self._scenes[name] = steps
        self.logger.info("Registered scene: %s with %d steps", name, len(steps))

    def execute_scene(self, scene_name: str) -> list[str]:
        """Execute a scene by name. Returns list of step results."""
        steps = self._scenes.get(scene_name)
        if not steps:
            return [f"Unknown scene: {scene_name}"]

        results = []
        for i, step in enumerate(steps, 1):
            try:
                result = self._execute_step(step)
                results.append(f"Step {i}: {result}")
            except Exception as exc:
                self.logger.exception("Scene %s step %d failed", scene_name, i)
                results.append(f"Step {i} failed: {exc}")
        return results

    def _execute_step(self, step: dict[str, Any]) -> str:
        """Execute a single automation step."""
        step_type = step.get("type")

        if step_type == "device":
            device_id = step["device_id"]
            action = step["action"]
            value = step.get("value")
            return self.device_manager.execute_command(device_id, action, value)

        elif step_type == "speak":
            text = step["text"]
            if self.tts_callback:
                self.tts_callback(text)
            return f"Spoke: {text}"

        elif step_type == "delay":
            seconds = float(step.get("seconds", 1))
            time.sleep(seconds)
            return f"Delayed {seconds}s"

        elif step_type == "oled":
            frame = step.get("frame", "idle")
            if self.event_bus:
                self.event_bus.emit("oled.set_frame", {"frame": frame})
            return f"Set OLED frame: {frame}"

        elif step_type == "event":
            event_name = step["event"]
            data = step.get("data")
            if self.event_bus:
                self.event_bus.emit(event_name, data)
            return f"Emitted event: {event_name}"

        else:
            return f"Unknown step type: {step_type}"

    def list_scenes(self) -> list[str]:
        """Return list of registered scene names."""
        return list(self._scenes.keys())
