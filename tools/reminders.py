"""Reminder tools — per-user reminders with JSON persistence."""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.tool_result import (
    FAILURE,
    NEEDS_CLARIFICATION,
    NOOP,
    OBSERVED,
    OUTCOME_FAILED,
    SUCCESS,
    make_tool_result,
)
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


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Core personal-assistant reminder creation capability.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Add missing_fields, timezone, recurrence, and due_at schema.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def create_reminder(content: str, remind_at: str = "") -> str:
    """Create a reminder for the current user."""
    content = content.strip()
    if not content:
        return make_tool_result(
            FAILURE,
            "Reminder content cannot be empty.",
            error_code="empty_content",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="reminder_store_ack",
            claim_policy=_failed_claim_policy(),
        )

    remind_at = remind_at.strip()
    if not remind_at:
        return make_tool_result(
            NEEDS_CLARIFICATION,
            "What time should I remind you?",
            data={
                "missing_fields": ["due_at"],
                "clarification": {
                    "question": "What time should I remind you?",
                    "missing_fields": ["due_at"],
                },
            },
            error_code="missing_due_at",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )
    user_text = str(_EXECUTION_CONTEXT.get("user_text") or "").strip()
    if user_text and not _user_text_has_explicit_time(user_text):
        return make_tool_result(
            NEEDS_CLARIFICATION,
            "What exact time should I remind you?",
            data={
                "remind_at": remind_at,
                "missing_fields": ["due_at.time"],
                "clarification": {
                    "question": "What exact time should I remind you?",
                    "missing_fields": ["due_at.time"],
                },
            },
            error_code="missing_explicit_time_in_user_text",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )
    try:
        due_at, timezone = _normalize_due_at(remind_at)
    except ValueError as exc:
        return make_tool_result(
            FAILURE,
            str(exc),
            data={"remind_at": remind_at},
            error_code="invalid_due_at",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )

    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"

    reminder = {
        "id": str(uuid.uuid4())[:8],
        "reminder_id": None,
        "user_id": user_id,
        "content": content,
        "remind_at": due_at,
        "due_at": due_at,
        "timezone": timezone,
        "is_done": False,
        "created_at": datetime.now().isoformat(),
    }
    reminder["reminder_id"] = reminder["id"]

    data = _load()
    data.append(reminder)
    _save(data)

    # Schedule the reminder if we have a scheduler and a time
    if _scheduler and getattr(_scheduler, "available", False):
        _schedule_reminder(reminder)

    return make_tool_result(
        SUCCESS,
        f"Reminder created (ID: {reminder['id']}): '{content}' for {due_at}.",
        data={
            "reminder": reminder,
            "reminder_id": reminder["id"],
            "title": content,
            "due_at": due_at,
            "timezone": timezone,
            "scheduled": True,
        },
        outcome_type="created",
        verified=True,
        verification_source="reminder_store_ack",
        claim_policy={
            "allowed_claims": ["reminder_created"],
            "forbidden_claims": [],
        },
    )


@jarvis_tool(
    read_only=True,
    lifecycle={
        "status": "active",
        "reason": "Low-risk read-only reminder query.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Improve voice/document rendering for list output.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "read_only",
        "risk_level": "low",
        "has_side_effects": False,
    },
)
def list_reminders() -> str:
    """List all active reminders for the current user."""
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load()
    user_reminders = [
        r for r in data
        if r.get("user_id") == user_id and not r.get("is_done", False)
    ]
    if not user_reminders:
        return make_tool_result(
            SUCCESS,
            "No active reminders.",
            data={"reminders": [], "count": 0},
            outcome_type=OBSERVED,
            verified=True,
            verification_source="reminder_store_read",
        )

    lines = []
    for r in user_reminders:
        time_part = f" (due: {r['remind_at']})" if r.get("remind_at") else ""
        lines.append(f"- [{r['id']}] {r['content']}{time_part}")
    return make_tool_result(
        SUCCESS,
        "Active reminders:\n" + "\n".join(lines),
        data={"reminders": user_reminders, "count": len(user_reminders)},
        outcome_type=OBSERVED,
        verified=True,
        verification_source="reminder_store_read",
    )


@jarvis_tool(
    read_only=False,
    lifecycle={
        "status": "active",
        "reason": "Low-risk completion action when reminder_id is explicit.",
        "reviewed_at": "2026-05-10",
        "phase3_action": "Add ambiguity handling for natural-language reminder references.",
        "replacement": None,
    },
    exposure={
        "expose_to_llm": True,
        "allow_regex": True,
        "allow_frontend_direct": False,
    },
    classification={
        "layer": "primitive",
        "primary": "state_changing",
        "risk_level": "low",
        "has_side_effects": True,
    },
)
def complete_reminder(reminder_id: str) -> str:
    """Mark a reminder as done by its ID."""
    reminder_id = reminder_id.strip()
    if not reminder_id:
        return make_tool_result(
            NEEDS_CLARIFICATION,
            "Which reminder should I complete?",
            data={
                "missing_fields": ["reminder_id"],
                "clarification": {
                    "question": "Which reminder should I complete?",
                    "missing_fields": ["reminder_id"],
                },
            },
            error_code="missing_reminder_id",
            outcome_type=OUTCOME_FAILED,
            verified=False,
            verification_source="none",
            claim_policy=_failed_claim_policy(),
        )
    user_id = _EXECUTION_CONTEXT.get("user_id") or "_anonymous"
    data = _load()
    for r in data:
        if r.get("id") == reminder_id and r.get("user_id") == user_id:
            if r.get("is_done", False):
                return make_tool_result(
                    NOOP,
                    f"Reminder '{r['content']}' was already done.",
                    data={"reminder": r, "reminder_id": reminder_id},
                    outcome_type="no_change",
                    verified=True,
                    verification_source="reminder_store_ack",
                    claim_policy={
                        "allowed_claims": ["reminder_already_completed"],
                        "forbidden_claims": ["reminder_completed"],
                    },
                )
            r["is_done"] = True
            r["completed_at"] = datetime.now().isoformat()
            _save(data)
            return make_tool_result(
                SUCCESS,
                f"Reminder '{r['content']}' marked as done.",
                data={
                    "reminder": r,
                    "reminder_id": reminder_id,
                    "completed_at": r["completed_at"],
                },
                outcome_type="updated",
                verified=True,
                verification_source="reminder_store_ack",
                claim_policy={
                    "allowed_claims": ["reminder_completed"],
                    "forbidden_claims": [],
                },
            )
    return make_tool_result(
        FAILURE,
        f"Reminder {reminder_id} not found.",
        data={"reminder_id": reminder_id},
        error_code="reminder_not_found",
        outcome_type=OUTCOME_FAILED,
        verified=False,
        verification_source="reminder_store_ack",
        claim_policy=_failed_claim_policy(),
    )


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


def _normalize_due_at(raw: str) -> tuple[str, str]:
    """Normalize an ISO-like reminder time and attach local timezone if absent."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("Reminder time must be an ISO-like datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    timezone = parsed.tzname() or "local"
    return parsed.isoformat(), timezone


def _user_text_has_explicit_time(text: str) -> bool:
    """Return True only when the user utterance includes a concrete time."""
    normalized = text.strip().lower()
    explicit_patterns = (
        r"\d{1,2}\s*[:：]\s*\d{2}",
        r"\d{1,2}\s*(?:am|pm)\b",
        r"\b(?:at|by)\s+\d{1,2}(?:\s*(?:am|pm))?\b",
        r"[零〇一二两三四五六七八九十\d]{1,3}\s*点(?:半|[零〇一二两三四五六七八九十\d]{1,3}\s*分?)?",
        r"\d{1,2}\s*点(?:半|\d{1,2}\s*分?)?",
        r"\d+\s*(?:秒|分钟|小时|天|周|礼拜|星期)\s*(?:后|以后|之后)",
        r"[零〇一二两三四五六七八九十两]+(?:个)?\s*(?:小时|分钟|天|周|礼拜|星期)\s*(?:后|以后|之后)",
    )
    return any(re.search(pattern, normalized) for pattern in explicit_patterns)


def _failed_claim_policy() -> dict[str, list[str]]:
    return {
        "allowed_claims": ["tool_failed_contract_validation"],
        "forbidden_claims": [
            "reminder_created",
            "reminder_completed",
            "reminder_already_completed",
        ],
    }


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
