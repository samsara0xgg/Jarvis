"""Jarvis scheduler — APScheduler wrapper with persistent job store."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class JarvisScheduler:
    """Background task scheduler backed by APScheduler + SQLite.

    Gracefully degrades if APScheduler is not installed.
    """

    def __init__(self, config: dict, event_bus: Any = None) -> None:
        self.config = config.get("scheduler", {})
        self.event_bus = event_bus
        self._scheduler: Any = None
        self._available = False
        self.logger = LOGGER

        db_path = Path(self.config.get("db_path", "data/scheduler.db"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_url = f"sqlite:///{db_path}"

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

            jobstores = {"default": SQLAlchemyJobStore(url=self._db_url)}
            self._scheduler = BackgroundScheduler(
                jobstores=jobstores,
                job_defaults={"coalesce": True, "max_instances": 1},
            )
            self._available = True
        except ImportError:
            self.logger.warning(
                "APScheduler not installed. Scheduler disabled. "
                "Install with: pip install apscheduler"
            )

    @property
    def available(self) -> bool:
        return self._available

    def start(self) -> None:
        """Start the background scheduler."""
        if not self._available:
            return
        if not self._scheduler.running:
            self._scheduler.start()
            self.logger.info("Scheduler started.")

    def stop(self) -> None:
        """Shut down the scheduler."""
        if self._available and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self.logger.info("Scheduler stopped.")

    def add_date_job(
        self,
        job_id: str,
        func: Callable,
        run_date: str,
        args: list | None = None,
        kwargs: dict | None = None,
        replace: bool = True,
    ) -> str | None:
        """Schedule a one-shot job at a specific datetime.

        Args:
            job_id: Unique job identifier.
            func: Callable to execute.
            run_date: ISO format datetime string.
            args: Positional arguments for func.
            kwargs: Keyword arguments for func.
            replace: If True, replace existing job with same ID.

        Returns:
            The job ID, or None if scheduler unavailable.
        """
        if not self._available:
            return None
        if replace:
            self.remove_job(job_id)
        job = self._scheduler.add_job(
            func,
            trigger="date",
            run_date=run_date,
            id=job_id,
            args=args or [],
            kwargs=kwargs or {},
        )
        self.logger.info("Scheduled date job %s at %s", job_id, run_date)
        return job.id

    def add_cron_job(
        self,
        job_id: str,
        func: Callable,
        *,
        hour: int | str = "*",
        minute: int | str = 0,
        day_of_week: str = "*",
        args: list | None = None,
        kwargs: dict | None = None,
        replace: bool = True,
    ) -> str | None:
        """Schedule a recurring cron job.

        Args:
            job_id: Unique job identifier.
            func: Callable to execute.
            hour: Hour(s) to run.
            minute: Minute(s) to run.
            day_of_week: Days to run (mon-sun or 0-6).
            args: Positional arguments for func.
            kwargs: Keyword arguments for func.
            replace: If True, replace existing job with same ID.

        Returns:
            The job ID, or None if scheduler unavailable.
        """
        if not self._available:
            return None
        if replace:
            self.remove_job(job_id)
        job = self._scheduler.add_job(
            func,
            trigger="cron",
            hour=hour,
            minute=minute,
            day_of_week=day_of_week,
            id=job_id,
            args=args or [],
            kwargs=kwargs or {},
        )
        self.logger.info(
            "Scheduled cron job %s at %s:%s on %s", job_id, hour, minute, day_of_week,
        )
        return job.id

    def add_interval_job(
        self,
        job_id: str,
        func: Callable,
        *,
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        args: list | None = None,
        kwargs: dict | None = None,
        replace: bool = True,
    ) -> str | None:
        """Schedule a recurring interval job."""
        if not self._available:
            return None
        if replace:
            self.remove_job(job_id)
        job = self._scheduler.add_job(
            func,
            trigger="interval",
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            id=job_id,
            args=args or [],
            kwargs=kwargs or {},
        )
        self.logger.info("Scheduled interval job %s", job_id)
        return job.id

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID. Returns True if removed."""
        if not self._available:
            return False
        try:
            self._scheduler.remove_job(job_id)
            return True
        except Exception:
            return False

    def get_jobs(self) -> list[dict[str, Any]]:
        """List all scheduled jobs."""
        if not self._available:
            return []
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get a single job by ID."""
        if not self._available:
            return None
        job = self._scheduler.get_job(job_id)
        if not job:
            return None
        return {
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        }
