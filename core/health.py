"""Component health tracking with circuit breaker and proactive probes.

Provides :class:`ComponentTracker` — a centralised registry that each
subsystem (TTS, ASR, LLM, intent router …) reports success/failure to.
The tracker maintains a three-state machine per component and emits
events on the :class:`EventBus` when transitions occur.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)


class ComponentStatus(str, Enum):
    """Three-state health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class ComponentState:
    """Per-component health snapshot."""

    name: str
    status: ComponentStatus = ComponentStatus.HEALTHY
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    last_success_time: float | None = None
    last_failure_time: float | None = None
    last_error: str | None = None
    cooldown_until: float = 0.0  # time.monotonic()


class ComponentTracker:
    """Track component health and implement circuit-breaker logic.

    Args:
        config: Application configuration dict.  Reads ``health.circuit_breaker``
            for *failure_threshold*, *unavailable_threshold*, and *cooldown_seconds*.
        event_bus: Optional event bus for emitting ``health.*`` events.
    """

    def __init__(
        self,
        config: dict,
        event_bus: Any | None = None,
    ) -> None:
        cb_cfg = config.get("health", {}).get("circuit_breaker", {})
        self._failure_threshold: int = int(cb_cfg.get("failure_threshold", 3))
        self._unavailable_threshold: int = int(cb_cfg.get("unavailable_threshold", 10))
        self._cooldown_seconds: float = float(cb_cfg.get("cooldown_seconds", 60))

        self._event_bus = event_bus
        self._states: dict[str, ComponentState] = {}
        self._probes: dict[str, Callable[[], bool]] = {}
        self._lock = threading.Lock()
        self.logger = LOGGER

    # ------------------------------------------------------------------
    # State access
    # ------------------------------------------------------------------

    def _get_or_create(self, component: str) -> ComponentState:
        """Return existing state or create a new HEALTHY one (caller holds lock)."""
        if component not in self._states:
            self._states[component] = ComponentState(name=component)
        return self._states[component]

    def get_status(self, component: str) -> ComponentState:
        """Return a *copy* of the current state for *component*."""
        with self._lock:
            state = self._get_or_create(component)
            # Return a shallow copy so callers cannot mutate internal state.
            return ComponentState(
                name=state.name,
                status=state.status,
                consecutive_failures=state.consecutive_failures,
                total_successes=state.total_successes,
                total_failures=state.total_failures,
                last_success_time=state.last_success_time,
                last_failure_time=state.last_failure_time,
                last_error=state.last_error,
                cooldown_until=state.cooldown_until,
            )

    def get_all_statuses(self) -> dict[str, ComponentState]:
        """Return copies of all tracked component states."""
        with self._lock:
            return {
                name: ComponentState(
                    name=s.name,
                    status=s.status,
                    consecutive_failures=s.consecutive_failures,
                    total_successes=s.total_successes,
                    total_failures=s.total_failures,
                    last_success_time=s.last_success_time,
                    last_failure_time=s.last_failure_time,
                    last_error=s.last_error,
                    cooldown_until=s.cooldown_until,
                )
                for name, s in self._states.items()
            }

    def get_health_summary(self) -> dict[str, Any]:
        """Return a JSON-friendly summary for the HealthSkill."""
        statuses = self.get_all_statuses()
        healthy = [n for n, s in statuses.items() if s.status == ComponentStatus.HEALTHY]
        degraded = [n for n, s in statuses.items() if s.status == ComponentStatus.DEGRADED]
        unavailable = [n for n, s in statuses.items() if s.status == ComponentStatus.UNAVAILABLE]
        return {
            "is_healthy": len(degraded) == 0 and len(unavailable) == 0,
            "components": {
                name: {
                    "status": s.status.value,
                    "consecutive_failures": s.consecutive_failures,
                    "last_error": s.last_error,
                }
                for name, s in statuses.items()
            },
            "healthy": healthy,
            "degraded": degraded,
            "unavailable": unavailable,
        }

    # ------------------------------------------------------------------
    # Recording results
    # ------------------------------------------------------------------

    def record_success(self, component: str) -> None:
        """Record a successful call for *component*."""
        with self._lock:
            state = self._get_or_create(component)
            old_status = state.status
            state.consecutive_failures = 0
            state.total_successes += 1
            state.last_success_time = time.monotonic()

            if old_status != ComponentStatus.HEALTHY:
                downtime = (
                    state.last_success_time - state.last_failure_time
                    if state.last_failure_time is not None
                    else 0.0
                )
                state.status = ComponentStatus.HEALTHY
                state.cooldown_until = 0.0
                self._emit_status_changed(component, old_status, ComponentStatus.HEALTHY)
                self._emit_recovery(component, downtime)

    def record_failure(self, component: str, error: str = "") -> None:
        """Record a failed call for *component*."""
        with self._lock:
            state = self._get_or_create(component)
            old_status = state.status
            state.consecutive_failures += 1
            state.total_failures += 1
            state.last_failure_time = time.monotonic()
            state.last_error = error or None

            new_status = old_status
            if (
                old_status == ComponentStatus.HEALTHY
                and state.consecutive_failures >= self._failure_threshold
            ):
                new_status = ComponentStatus.DEGRADED
                state.cooldown_until = time.monotonic() + self._cooldown_seconds
            elif (
                old_status == ComponentStatus.DEGRADED
                and state.consecutive_failures >= self._unavailable_threshold
            ):
                new_status = ComponentStatus.UNAVAILABLE

            if new_status != old_status:
                state.status = new_status
                self._emit_status_changed(component, old_status, new_status, error)

    # ------------------------------------------------------------------
    # Availability query (used by fallback chains)
    # ------------------------------------------------------------------

    def is_available(self, component: str) -> bool:
        """Check whether *component* should be attempted.

        - HEALTHY → True
        - DEGRADED + within cooldown → False
        - DEGRADED + cooldown expired → True (allow one probe attempt)
        - UNAVAILABLE → always False (only probe recovery can restore)
        """
        with self._lock:
            state = self._get_or_create(component)
            if state.status == ComponentStatus.HEALTHY:
                return True
            if state.status == ComponentStatus.UNAVAILABLE:
                return False
            # DEGRADED
            return time.monotonic() > state.cooldown_until

    # ------------------------------------------------------------------
    # Probes
    # ------------------------------------------------------------------

    def register_probe(
        self,
        component: str,
        probe_fn: Callable[[], bool],
    ) -> None:
        """Register a health-check probe for *component*."""
        with self._lock:
            self._probes[component] = probe_fn
            # Ensure the component has an entry.
            self._get_or_create(component)

    def run_probe(self, component: str) -> bool:
        """Run the probe for *component* and update its state."""
        with self._lock:
            probe_fn = self._probes.get(component)
        if probe_fn is None:
            return True  # No probe registered — assume healthy.

        try:
            ok = probe_fn()
        except Exception as exc:
            self.logger.warning("Probe for %s raised: %s", component, exc)
            ok = False

        if ok:
            self.record_success(component)
        else:
            self.record_failure(component, "probe failed")
        return ok

    def run_all_probes(self) -> dict[str, bool]:
        """Run all registered probes and return results."""
        with self._lock:
            probe_names = list(self._probes.keys())

        results: dict[str, bool] = {}
        for name in probe_names:
            results[name] = self.run_probe(name)

        if self._event_bus:
            try:
                self._event_bus.emit("health.check_completed", {
                    "results": results,
                    "timestamp": time.time(),
                })
            except Exception:
                self.logger.exception("Failed to emit health.check_completed")

        return results

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self, component: str) -> None:
        """Manually reset *component* to HEALTHY."""
        with self._lock:
            state = self._get_or_create(component)
            old_status = state.status
            state.status = ComponentStatus.HEALTHY
            state.consecutive_failures = 0
            state.cooldown_until = 0.0
            if old_status != ComponentStatus.HEALTHY:
                self._emit_status_changed(component, old_status, ComponentStatus.HEALTHY)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _emit_status_changed(
        self,
        component: str,
        old_status: ComponentStatus,
        new_status: ComponentStatus,
        error: str = "",
    ) -> None:
        self.logger.info(
            "Component %s: %s → %s%s",
            component,
            old_status.value,
            new_status.value,
            f" ({error})" if error else "",
        )
        if self._event_bus:
            try:
                self._event_bus.emit("health.status_changed", {
                    "component": component,
                    "old_status": old_status.value,
                    "new_status": new_status.value,
                    "error": error,
                    "timestamp": time.time(),
                })
            except Exception:
                self.logger.exception("Failed to emit health.status_changed")

    def _emit_recovery(self, component: str, downtime_seconds: float) -> None:
        self.logger.info(
            "Component %s recovered (downtime %.1fs)", component, downtime_seconds,
        )
        if self._event_bus:
            try:
                self._event_bus.emit("health.recovery", {
                    "component": component,
                    "downtime_seconds": round(downtime_seconds, 1),
                    "timestamp": time.time(),
                })
            except Exception:
                self.logger.exception("Failed to emit health.recovery")
