"""Tests for core.health.ComponentTracker."""

import threading
import time
from unittest.mock import MagicMock, patch

from typing import Optional

import pytest

from core.event_bus import EventBus
from core.health import ComponentStatus, ComponentState, ComponentTracker


def _make_tracker(
    failure_threshold: int = 3,
    unavailable_threshold: int = 10,
    cooldown_seconds: float = 60,
    event_bus: Optional[EventBus] = None,
) -> ComponentTracker:
    config = {
        "health": {
            "circuit_breaker": {
                "failure_threshold": failure_threshold,
                "unavailable_threshold": unavailable_threshold,
                "cooldown_seconds": cooldown_seconds,
            },
        },
    }
    return ComponentTracker(config, event_bus=event_bus)


class TestInitialState:
    def test_new_component_is_healthy(self):
        tracker = _make_tracker()
        state = tracker.get_status("tts.openai")
        assert state.status == ComponentStatus.HEALTHY
        assert state.consecutive_failures == 0

    def test_is_available_for_unknown_component(self):
        tracker = _make_tracker()
        assert tracker.is_available("never.seen") is True


class TestRecordSuccess:
    def test_keeps_healthy(self):
        tracker = _make_tracker()
        tracker.record_success("tts.openai")
        state = tracker.get_status("tts.openai")
        assert state.status == ComponentStatus.HEALTHY
        assert state.total_successes == 1
        assert state.last_success_time is not None

    def test_resets_consecutive_failures(self):
        tracker = _make_tracker(failure_threshold=5)
        tracker.record_failure("c")
        tracker.record_failure("c")
        tracker.record_success("c")
        state = tracker.get_status("c")
        assert state.consecutive_failures == 0
        assert state.total_failures == 2
        assert state.total_successes == 1


class TestRecordFailure:
    def test_below_threshold_stays_healthy(self):
        tracker = _make_tracker(failure_threshold=3)
        tracker.record_failure("c")
        tracker.record_failure("c")
        state = tracker.get_status("c")
        assert state.status == ComponentStatus.HEALTHY
        assert state.consecutive_failures == 2

    def test_at_threshold_transitions_to_degraded(self):
        tracker = _make_tracker(failure_threshold=3)
        for _ in range(3):
            tracker.record_failure("c")
        state = tracker.get_status("c")
        assert state.status == ComponentStatus.DEGRADED

    def test_at_unavailable_threshold_transitions(self):
        tracker = _make_tracker(failure_threshold=3, unavailable_threshold=5)
        for _ in range(5):
            tracker.record_failure("c")
        state = tracker.get_status("c")
        assert state.status == ComponentStatus.UNAVAILABLE

    def test_stores_last_error(self):
        tracker = _make_tracker()
        tracker.record_failure("c", "timeout")
        state = tracker.get_status("c")
        assert state.last_error == "timeout"

    def test_empty_error_stored_as_none(self):
        tracker = _make_tracker()
        tracker.record_failure("c", "")
        state = tracker.get_status("c")
        assert state.last_error is None


class TestIsAvailable:
    def test_healthy_returns_true(self):
        tracker = _make_tracker()
        assert tracker.is_available("c") is True

    def test_degraded_within_cooldown_returns_false(self):
        tracker = _make_tracker(failure_threshold=2, cooldown_seconds=60)
        tracker.record_failure("c")
        tracker.record_failure("c")
        assert tracker.is_available("c") is False

    def test_degraded_after_cooldown_returns_true(self):
        tracker = _make_tracker(failure_threshold=2, cooldown_seconds=0.0)
        tracker.record_failure("c")
        tracker.record_failure("c")
        # cooldown_seconds=0 means cooldown expires immediately
        assert tracker.is_available("c") is True

    def test_unavailable_always_returns_false(self):
        tracker = _make_tracker(failure_threshold=2, unavailable_threshold=3, cooldown_seconds=0.0)
        for _ in range(3):
            tracker.record_failure("c")
        assert tracker.get_status("c").status == ComponentStatus.UNAVAILABLE
        assert tracker.is_available("c") is False


class TestRecovery:
    def test_degraded_to_healthy_on_success(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.record_failure("c")
        tracker.record_failure("c")
        assert tracker.get_status("c").status == ComponentStatus.DEGRADED
        tracker.record_success("c")
        assert tracker.get_status("c").status == ComponentStatus.HEALTHY

    def test_unavailable_to_healthy_on_success(self):
        tracker = _make_tracker(failure_threshold=2, unavailable_threshold=3)
        for _ in range(3):
            tracker.record_failure("c")
        assert tracker.get_status("c").status == ComponentStatus.UNAVAILABLE
        tracker.record_success("c")
        assert tracker.get_status("c").status == ComponentStatus.HEALTHY


class TestEvents:
    def test_status_changed_event_on_degradation(self):
        bus = EventBus()
        tracker = _make_tracker(failure_threshold=2, event_bus=bus)
        events: list[dict] = []
        bus.on("health.status_changed", lambda d: events.append(d))

        tracker.record_failure("c")
        assert len(events) == 0  # Not yet at threshold.
        tracker.record_failure("c")
        assert len(events) == 1
        assert events[0]["component"] == "c"
        assert events[0]["old_status"] == "healthy"
        assert events[0]["new_status"] == "degraded"

    def test_recovery_event_on_success_after_degraded(self):
        bus = EventBus()
        tracker = _make_tracker(failure_threshold=2, event_bus=bus)
        recovery_events: list[dict] = []
        bus.on("health.recovery", lambda d: recovery_events.append(d))

        tracker.record_failure("c")
        tracker.record_failure("c")
        tracker.record_success("c")
        assert len(recovery_events) == 1
        assert recovery_events[0]["component"] == "c"
        assert "downtime_seconds" in recovery_events[0]

    def test_no_event_when_staying_healthy(self):
        bus = EventBus()
        tracker = _make_tracker(event_bus=bus)
        events: list[dict] = []
        bus.on("health.status_changed", lambda d: events.append(d))
        tracker.record_success("c")
        tracker.record_success("c")
        assert len(events) == 0


class TestProbes:
    def test_register_and_run_probe_success(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.register_probe("c", lambda: True)
        assert tracker.run_probe("c") is True
        state = tracker.get_status("c")
        assert state.total_successes == 1

    def test_probe_failure_records_failure(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.register_probe("c", lambda: False)
        assert tracker.run_probe("c") is False
        state = tracker.get_status("c")
        assert state.total_failures == 1

    def test_probe_exception_counts_as_failure(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.register_probe("c", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert tracker.run_probe("c") is False

    def test_run_all_probes(self):
        bus = EventBus()
        tracker = _make_tracker(event_bus=bus)
        check_events: list[dict] = []
        bus.on("health.check_completed", lambda d: check_events.append(d))

        tracker.register_probe("a", lambda: True)
        tracker.register_probe("b", lambda: False)
        results = tracker.run_all_probes()
        assert results == {"a": True, "b": False}
        assert len(check_events) == 1
        assert check_events[0]["results"] == {"a": True, "b": False}

    def test_no_probe_returns_true(self):
        tracker = _make_tracker()
        assert tracker.run_probe("unregistered") is True

    def test_probe_recovers_degraded_component(self):
        tracker = _make_tracker(failure_threshold=2, cooldown_seconds=0.0)
        tracker.record_failure("c")
        tracker.record_failure("c")
        assert tracker.get_status("c").status == ComponentStatus.DEGRADED

        tracker.register_probe("c", lambda: True)
        tracker.run_probe("c")
        assert tracker.get_status("c").status == ComponentStatus.HEALTHY


class TestGetAllAndSummary:
    def test_get_all_statuses(self):
        tracker = _make_tracker()
        tracker.record_success("a")
        tracker.record_failure("b")
        statuses = tracker.get_all_statuses()
        assert set(statuses.keys()) == {"a", "b"}

    def test_get_health_summary_all_healthy(self):
        tracker = _make_tracker()
        tracker.record_success("a")
        summary = tracker.get_health_summary()
        assert summary["is_healthy"] is True
        assert summary["healthy"] == ["a"]
        assert summary["degraded"] == []
        assert summary["unavailable"] == []

    def test_get_health_summary_with_degraded(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.record_success("a")
        tracker.record_failure("b")
        tracker.record_failure("b")
        summary = tracker.get_health_summary()
        assert summary["is_healthy"] is False
        assert "a" in summary["healthy"]
        assert "b" in summary["degraded"]


class TestReset:
    def test_reset_to_healthy(self):
        tracker = _make_tracker(failure_threshold=2)
        tracker.record_failure("c")
        tracker.record_failure("c")
        assert tracker.get_status("c").status == ComponentStatus.DEGRADED
        tracker.reset("c")
        state = tracker.get_status("c")
        assert state.status == ComponentStatus.HEALTHY
        assert state.consecutive_failures == 0

    def test_reset_emits_event(self):
        bus = EventBus()
        tracker = _make_tracker(failure_threshold=2, event_bus=bus)
        events: list[dict] = []
        bus.on("health.status_changed", lambda d: events.append(d))

        tracker.record_failure("c")
        tracker.record_failure("c")
        events.clear()
        tracker.reset("c")
        assert len(events) == 1
        assert events[0]["new_status"] == "healthy"


class TestThreadSafety:
    def test_concurrent_record_calls(self):
        tracker = _make_tracker(failure_threshold=100)
        errors: list[Exception] = []

        def record_many(fn, n):
            try:
                for _ in range(n):
                    fn("c")
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=record_many, args=(tracker.record_success, 100)),
            threading.Thread(target=record_many, args=(tracker.record_failure, 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        state = tracker.get_status("c")
        assert state.total_successes + state.total_failures == 150


class TestConfigDefaults:
    def test_empty_config_uses_defaults(self):
        tracker = ComponentTracker({})
        assert tracker._failure_threshold == 3
        assert tracker._unavailable_threshold == 10
        assert tracker._cooldown_seconds == 60.0

    def test_tracker_works_without_event_bus(self):
        tracker = ComponentTracker({})
        tracker.record_failure("c")
        tracker.record_success("c")
        assert tracker.get_status("c").status == ComponentStatus.HEALTHY


class TestGetStatusReturnsCopy:
    def test_mutation_does_not_affect_internal(self):
        tracker = _make_tracker()
        state = tracker.get_status("c")
        state.consecutive_failures = 999
        assert tracker.get_status("c").consecutive_failures == 0
