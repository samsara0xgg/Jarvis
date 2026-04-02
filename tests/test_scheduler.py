"""Tests for JarvisScheduler — works with or without APScheduler installed."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.scheduler import JarvisScheduler


def _make_config(tmp_path):
    return {"scheduler": {"db_path": str(tmp_path / "sched.db")}}


class TestSchedulerUnavailable:
    """Tests when APScheduler is not installed (graceful degradation)."""

    def test_available_false_without_apscheduler(self, tmp_path) -> None:
        with patch.dict("sys.modules", {"apscheduler": None, "apscheduler.schedulers.background": None, "apscheduler.jobstores.sqlalchemy": None}):
            # Force ImportError by making the import fail
            sched = JarvisScheduler.__new__(JarvisScheduler)
            sched.config = {}
            sched.event_bus = None
            sched._scheduler = None
            sched._available = False
            sched.logger = MagicMock()
            assert sched.available is False

    def test_start_noop_when_unavailable(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched._scheduler = None
        sched.logger = MagicMock()
        sched.start()  # should not raise

    def test_stop_noop_when_unavailable(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched._scheduler = None
        sched.logger = MagicMock()
        sched.stop()  # should not raise

    def test_add_date_job_returns_none(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.add_date_job("j1", lambda: None, "2026-01-01") is None

    def test_add_cron_job_returns_none(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.add_cron_job("j1", lambda: None) is None

    def test_add_interval_job_returns_none(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.add_interval_job("j1", lambda: None, seconds=60) is None

    def test_remove_job_returns_false(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.remove_job("j1") is False

    def test_get_jobs_returns_empty(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.get_jobs() == []

    def test_get_job_returns_none(self, tmp_path) -> None:
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = False
        sched.logger = MagicMock()
        assert sched.get_job("j1") is None


class TestSchedulerWithMock:
    """Tests with a mocked APScheduler backend."""

    def _make_scheduler(self):
        sched = JarvisScheduler.__new__(JarvisScheduler)
        sched._available = True
        sched.logger = MagicMock()
        sched._scheduler = MagicMock()
        sched._scheduler.running = False
        return sched

    def test_start(self) -> None:
        sched = self._make_scheduler()
        sched.start()
        sched._scheduler.start.assert_called_once()

    def test_start_already_running(self) -> None:
        sched = self._make_scheduler()
        sched._scheduler.running = True
        sched.start()
        sched._scheduler.start.assert_not_called()

    def test_stop(self) -> None:
        sched = self._make_scheduler()
        sched._scheduler.running = True
        sched.stop()
        sched._scheduler.shutdown.assert_called_once_with(wait=False)

    def test_stop_not_running(self) -> None:
        sched = self._make_scheduler()
        sched._scheduler.running = False
        sched.stop()
        sched._scheduler.shutdown.assert_not_called()

    def test_add_date_job(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "test_date"
        sched._scheduler.add_job.return_value = mock_job

        result = sched.add_date_job("test_date", lambda: None, "2026-12-31")
        assert result == "test_date"
        sched._scheduler.add_job.assert_called_once()

    def test_add_cron_job(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "cron1"
        sched._scheduler.add_job.return_value = mock_job

        result = sched.add_cron_job("cron1", lambda: None, hour=7, minute=30, day_of_week="mon-fri")
        assert result == "cron1"

    def test_add_interval_job(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "interval1"
        sched._scheduler.add_job.return_value = mock_job

        result = sched.add_interval_job("interval1", lambda: None, minutes=30)
        assert result == "interval1"

    def test_remove_job_success(self) -> None:
        sched = self._make_scheduler()
        assert sched.remove_job("j1") is True

    def test_remove_job_not_found(self) -> None:
        sched = self._make_scheduler()
        sched._scheduler.remove_job.side_effect = KeyError("not found")
        assert sched.remove_job("missing") is False

    def test_get_jobs(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "j1"
        mock_job.name = "test"
        mock_job.next_run_time = None
        mock_job.trigger = "cron"
        sched._scheduler.get_jobs.return_value = [mock_job]

        jobs = sched.get_jobs()
        assert len(jobs) == 1
        assert jobs[0]["id"] == "j1"

    def test_get_job_found(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "j1"
        mock_job.name = "test"
        mock_job.next_run_time = None
        mock_job.trigger = "date"
        sched._scheduler.get_job.return_value = mock_job

        result = sched.get_job("j1")
        assert result["id"] == "j1"

    def test_get_job_not_found(self) -> None:
        sched = self._make_scheduler()
        sched._scheduler.get_job.return_value = None
        assert sched.get_job("missing") is None

    def test_replace_removes_existing(self) -> None:
        sched = self._make_scheduler()
        mock_job = MagicMock()
        mock_job.id = "j1"
        sched._scheduler.add_job.return_value = mock_job

        sched.add_date_job("j1", lambda: None, "2026-01-01", replace=True)
        sched._scheduler.remove_job.assert_called_once_with("j1")
