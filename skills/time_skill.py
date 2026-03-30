"""Time skill — current time, date, and simple timers."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class TimeSkill(Skill):
    """Provides time/date queries and simple countdown timers."""

    def __init__(self, config: dict) -> None:
        self._active_timers: dict[str, threading.Timer] = {}
        self._timer_callbacks: Any = None  # set by JarvisApp for TTS announcements
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "time"

    def set_timer_callback(self, callback: Any) -> None:
        """Set a callback for when timers fire (e.g. TTS announcement)."""
        self._timer_callbacks = callback

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "get_current_time",
                "description": "Get the current date and time.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "set_timer",
                "description": "Set a countdown timer. When it fires, Jarvis will announce it.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "integer",
                            "description": "Timer duration in seconds.",
                        },
                        "label": {
                            "type": "string",
                            "description": "What the timer is for, e.g. 'pasta' or 'break'.",
                        },
                    },
                    "required": ["seconds"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        if tool_name == "get_current_time":
            now = datetime.now()
            return now.strftime("Current time: %Y-%m-%d %H:%M:%S (%A)")

        if tool_name == "set_timer":
            seconds = int(tool_input.get("seconds", 0))
            label = str(tool_input.get("label", "timer"))
            if seconds <= 0:
                return "Timer duration must be positive."
            return self._start_timer(seconds, label)

        return f"Unknown time tool: {tool_name}"

    def _start_timer(self, seconds: int, label: str) -> str:
        timer_id = f"{label}_{seconds}"

        def _on_fire() -> None:
            self._active_timers.pop(timer_id, None)
            message = f"Timer '{label}' ({seconds} seconds) has finished!"
            self.logger.info(message)
            if self._timer_callbacks:
                try:
                    self._timer_callbacks(message)
                except Exception as exc:
                    self.logger.warning("Timer callback failed: %s", exc)

        if timer_id in self._active_timers:
            self._active_timers[timer_id].cancel()

        timer = threading.Timer(seconds, _on_fire)
        timer.daemon = True
        timer.start()
        self._active_timers[timer_id] = timer

        if seconds >= 60:
            display = f"{seconds // 60} minutes {seconds % 60} seconds"
        else:
            display = f"{seconds} seconds"
        return f"Timer set: '{label}' for {display}."

    def cancel_all(self) -> None:
        """Cancel all active timers."""
        for timer in self._active_timers.values():
            timer.cancel()
        self._active_timers.clear()
