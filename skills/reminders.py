"""Reminders skill — per-user reminders with JSON persistence and scheduler integration."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from skills import Skill

LOGGER = logging.getLogger(__name__)


class ReminderSkill(Skill):
    """Per-user reminder management with persistent JSON storage.

    When a scheduler is provided, reminders with ``remind_at`` are
    registered as real timed jobs that fire via TTS.
    """

    def __init__(
        self,
        config: dict,
        scheduler: Any = None,
        tts_callback: Callable[[str], None] | None = None,
        event_bus: Any = None,
    ) -> None:
        self.filepath = Path(
            config.get("skills", {}).get("reminders", {}).get("path", "data/reminders.json")
        )
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._scheduler = scheduler
        self._tts_callback = tts_callback
        self._event_bus = event_bus
        self.logger = LOGGER

        # Re-register persisted reminders with the scheduler on startup
        if self._scheduler and self._scheduler.available:
            self._restore_scheduled_reminders()

    @property
    def skill_name(self) -> str:
        return "reminders"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "create_reminder",
                "description": "Create a reminder for the current user.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "What to be reminded about.",
                        },
                        "remind_at": {
                            "type": "string",
                            "description": "When to remind, ISO format or natural like '2025-03-28 15:00'. Optional.",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "list_reminders",
                "description": "List all active reminders for the current user.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "complete_reminder",
                "description": "Mark a reminder as done by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reminder_id": {
                            "type": "string",
                            "description": "The reminder ID to complete.",
                        },
                    },
                    "required": ["reminder_id"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        user_id = context.get("user_id") or "_anonymous"

        if tool_name == "create_reminder":
            return self._create(user_id, tool_input)
        if tool_name == "list_reminders":
            return self._list(user_id)
        if tool_name == "complete_reminder":
            return self._complete(user_id, tool_input)
        return f"Unknown reminder tool: {tool_name}"

    def _create(self, user_id: str, tool_input: dict[str, Any]) -> str:
        content = str(tool_input.get("content", "")).strip()
        if not content:
            return "Reminder content cannot be empty."

        remind_at = str(tool_input.get("remind_at", "")).strip() or None
        reminder = {
            "id": str(uuid.uuid4())[:8],
            "user_id": user_id,
            "content": content,
            "remind_at": remind_at,
            "is_done": False,
            "created_at": datetime.now().isoformat(),
        }

        data = self._load()
        data.append(reminder)
        self._save(data)

        # Schedule the reminder if we have a scheduler and a time
        if remind_at and self._scheduler and self._scheduler.available:
            self._schedule_reminder(reminder)

        time_part = f" for {remind_at}" if remind_at else ""
        return f"Reminder created (ID: {reminder['id']}): '{content}'{time_part}."

    def _list(self, user_id: str) -> str:
        data = self._load()
        user_reminders = [
            r for r in data
            if r.get("user_id") == user_id and not r.get("is_done", False)
        ]
        if not user_reminders:
            return "No active reminders."

        lines = []
        for r in user_reminders:
            time_part = f" (due: {r['remind_at']})" if r.get("remind_at") else ""
            lines.append(f"- [{r['id']}] {r['content']}{time_part}")
        return "Active reminders:\n" + "\n".join(lines)

    def _complete(self, user_id: str, tool_input: dict[str, Any]) -> str:
        reminder_id = str(tool_input.get("reminder_id", "")).strip()
        data = self._load()
        for r in data:
            if r.get("id") == reminder_id and r.get("user_id") == user_id:
                r["is_done"] = True
                self._save(data)
                return f"Reminder '{r['content']}' marked as done."
        return f"Reminder {reminder_id} not found."

    def _load(self) -> list[dict[str, Any]]:
        if not self.filepath.exists():
            return []
        try:
            with self.filepath.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, data: list[dict[str, Any]]) -> None:
        with self.filepath.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _schedule_reminder(self, reminder: dict[str, Any]) -> None:
        """Register a reminder as a scheduled job."""
        try:
            self._scheduler.add_date_job(
                job_id=f"reminder_{reminder['id']}",
                func=_fire_reminder,
                run_date=reminder["remind_at"],
                kwargs={
                    "reminder_id": reminder["id"],
                    "content": reminder["content"],
                    "tts_callback": self._tts_callback,
                    "event_bus": self._event_bus,
                },
            )
            self.logger.info("Scheduled reminder %s at %s", reminder["id"], reminder["remind_at"])
        except Exception as exc:
            self.logger.warning("Failed to schedule reminder %s: %s", reminder["id"], exc)

    def _restore_scheduled_reminders(self) -> None:
        """Re-register all pending timed reminders with the scheduler on startup."""
        data = self._load()
        now = datetime.now()
        for r in data:
            if r.get("is_done") or not r.get("remind_at"):
                continue
            try:
                run_date = datetime.fromisoformat(r["remind_at"])
                if run_date > now:
                    self._schedule_reminder(r)
            except (ValueError, TypeError):
                pass


def _fire_reminder(
    reminder_id: str,
    content: str,
    tts_callback: Callable[[str], None] | None = None,
    event_bus: Any = None,
) -> None:
    """Callback executed when a scheduled reminder fires."""
    LOGGER.info("Reminder fired: [%s] %s", reminder_id, content)
    message = f"Reminder: {content}"
    if tts_callback:
        try:
            tts_callback(message)
        except Exception:
            LOGGER.exception("TTS failed for reminder %s", reminder_id)
    if event_bus:
        event_bus.emit("reminder.fired", {"id": reminder_id, "content": content})
