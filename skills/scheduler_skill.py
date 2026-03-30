"""Scheduler skill — schedule tasks, manage jobs, morning briefings."""

from __future__ import annotations

import logging
from typing import Any

from skills import Skill

LOGGER = logging.getLogger(__name__)


class SchedulerSkill(Skill):
    """LLM-callable skill for scheduling one-shot and recurring tasks."""

    def __init__(self, config: dict, scheduler: Any = None) -> None:
        self._scheduler = scheduler
        self._config = config.get("scheduler", {})
        self.logger = LOGGER

    @property
    def skill_name(self) -> str:
        return "scheduler"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "schedule_task",
                "description": (
                    "Schedule a task to run at a specific time or on a recurring schedule. "
                    "For one-shot tasks, provide run_at. For recurring, provide cron fields."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Unique identifier for this task.",
                        },
                        "description": {
                            "type": "string",
                            "description": "What this task does (shown when it fires).",
                        },
                        "run_at": {
                            "type": "string",
                            "description": "ISO datetime for one-shot task, e.g. '2025-03-28T15:00:00'.",
                        },
                        "cron_hour": {
                            "type": "string",
                            "description": "Hour for recurring task (0-23 or *).",
                        },
                        "cron_minute": {
                            "type": "string",
                            "description": "Minute for recurring task (0-59 or *).",
                        },
                        "cron_day_of_week": {
                            "type": "string",
                            "description": "Day of week (mon-sun or * for daily).",
                        },
                    },
                    "required": ["task_id", "description"],
                },
            },
            {
                "name": "list_scheduled_tasks",
                "description": "List all scheduled tasks.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "cancel_scheduled_task",
                "description": "Cancel a scheduled task by its ID.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "The task ID to cancel.",
                        },
                    },
                    "required": ["task_id"],
                },
            },
        ]

    def execute(self, tool_name: str, tool_input: dict[str, Any], **context: Any) -> str:
        if tool_name == "schedule_task":
            return self._schedule(tool_input)
        if tool_name == "list_scheduled_tasks":
            return self._list()
        if tool_name == "cancel_scheduled_task":
            return self._cancel(tool_input)
        return f"Unknown scheduler tool: {tool_name}"

    def _schedule(self, tool_input: dict[str, Any]) -> str:
        if not self._scheduler or not self._scheduler.available:
            return "Scheduler is not available. Install apscheduler to enable."

        task_id = str(tool_input.get("task_id", "")).strip()
        description = str(tool_input.get("description", "")).strip()
        if not task_id or not description:
            return "task_id and description are required."

        run_at = tool_input.get("run_at")
        cron_hour = tool_input.get("cron_hour")

        if run_at:
            job_id = self._scheduler.add_date_job(
                job_id=f"skill_{task_id}",
                func=_scheduled_task_callback,
                run_date=run_at,
                kwargs={"task_id": task_id, "description": description},
            )
            if job_id:
                return f"Scheduled one-shot task '{description}' at {run_at} (ID: {task_id})."
            return "Failed to schedule task."
        elif cron_hour is not None:
            cron_minute = tool_input.get("cron_minute", "0")
            cron_dow = tool_input.get("cron_day_of_week", "*")
            job_id = self._scheduler.add_cron_job(
                job_id=f"skill_{task_id}",
                func=_scheduled_task_callback,
                hour=cron_hour,
                minute=cron_minute,
                day_of_week=cron_dow,
                kwargs={"task_id": task_id, "description": description},
            )
            if job_id:
                return (
                    f"Scheduled recurring task '{description}' "
                    f"at {cron_hour}:{cron_minute} on {cron_dow} (ID: {task_id})."
                )
            return "Failed to schedule task."
        else:
            return "Provide either run_at (one-shot) or cron_hour (recurring)."

    def _list(self) -> str:
        if not self._scheduler or not self._scheduler.available:
            return "Scheduler is not available."
        jobs = self._scheduler.get_jobs()
        if not jobs:
            return "No scheduled tasks."
        lines = []
        for job in jobs:
            next_run = job.get("next_run", "unknown")
            lines.append(f"- [{job['id']}] {job['name']} — next: {next_run}")
        return "Scheduled tasks:\n" + "\n".join(lines)

    def _cancel(self, tool_input: dict[str, Any]) -> str:
        if not self._scheduler or not self._scheduler.available:
            return "Scheduler is not available."
        task_id = str(tool_input.get("task_id", "")).strip()
        if not task_id:
            return "task_id is required."
        # Try both with and without prefix
        removed = self._scheduler.remove_job(f"skill_{task_id}")
        if not removed:
            removed = self._scheduler.remove_job(task_id)
        if removed:
            return f"Cancelled task {task_id}."
        return f"Task {task_id} not found."


def _scheduled_task_callback(task_id: str, description: str) -> None:
    """Default callback for scheduled tasks — logs the event."""
    LOGGER.info("Scheduled task fired: [%s] %s", task_id, description)
