"""Reminder tools — per-user reminders with JSON persistence."""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tools import jarvis_tool, _EXECUTION_CONTEXT

LOGGER = logging.getLogger(__name__)

_filepath: Path = Path("data/reminders.json")
_scheduler: Any = None
_tts_callback: Callable | None = None
_event_bus: Any = None


def init(
    filepath: str = "data/reminders.json",
    scheduler: Any = None,
    tts_callback: Any = None,
    event_bus: Any = None,
) -> None:
    """Initialize reminder module with persistence path and optional scheduler."""
    global _filepath, _scheduler, _tts_callback, _event_bus
    _filepath = Path(filepath)
    _filepath.parent.mkdir(parents=True, exist_ok=True)
    _scheduler = scheduler
    _tts_callback = tts_callback
    _event_bus = event_bus
    if _scheduler and getattr(_scheduler, "available", False):
        _restore_scheduled_reminders()


@jarvis_tool(read_only=False)
def create_reminder(content: str, remind_at: str = "") -> str:
    """Create a reminder for the current user."""
    content = content.strip()
    if not content:
        return "Reminder content cannot be empty."

    remind_at = remind_at.strip() or None
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"

    reminder = {
        "id": str(uuid.uuid4())[:8],
        "user_id": user_id,
        "content": content,
        "remind_at": remind_at,
        "is_done": False,
        "created_at": datetime.now().isoformat(),
    }

    data = _load()
    data.append(reminder)
    _save(data)

    # Schedule the reminder if we have a scheduler and a time
    if remind_at and _scheduler and getattr(_scheduler, "available", False):
        _schedule_reminder(reminder)

    time_part = f" for {remind_at}" if remind_at else ""
    return f"Reminder created (ID: {reminder['id']}): '{content}'{time_part}."


@jarvis_tool(read_only=True)
def list_reminders() -> str:
    """List all active reminders for the current user."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load()
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


@jarvis_tool(read_only=False)
def complete_reminder(reminder_id: str) -> str:
    """Mark a reminder as done by its ID."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load()
    for r in data:
        if r.get("id") == reminder_id and r.get("user_id") == user_id:
            r["is_done"] = True
            _save(data)
            return f"Reminder '{r['content']}' marked as done."
    return f"Reminder {reminder_id} not found."


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load() -> list[dict[str, Any]]:
    """Load reminders from JSON file."""
    if not _filepath.exists():
        return []
    try:
        with _filepath.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(data: list[dict[str, Any]]) -> None:
    """Save reminders to JSON file."""
    with _filepath.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _schedule_reminder(reminder: dict[str, Any]) -> None:
    """Register a reminder as a scheduled job."""
    try:
        _scheduler.add_date_job(
            job_id=f"reminder_{reminder['id']}",
            func=_fire_reminder,
            run_date=reminder["remind_at"],
            kwargs={
                "reminder_id": reminder["id"],
                "content": reminder["content"],
                "tts_callback": _tts_callback,
                "event_bus": _event_bus,
            },
        )
        LOGGER.info("Scheduled reminder %s at %s", reminder["id"], reminder["remind_at"])
    except Exception as exc:
        LOGGER.warning("Failed to schedule reminder %s: %s", reminder["id"], exc)


def _restore_scheduled_reminders() -> None:
    """Re-register all pending timed reminders with the scheduler on startup."""
    data = _load()
    now = datetime.now()
    for r in data:
        if r.get("is_done") or not r.get("remind_at"):
            continue
        try:
            run_date = datetime.fromisoformat(r["remind_at"])
            if run_date > now:
                _schedule_reminder(r)
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
