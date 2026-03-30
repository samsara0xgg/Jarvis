"""Lightweight pub/sub event bus for cross-module communication."""

from __future__ import annotations

import fnmatch
import logging
import threading
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

Listener = Callable[..., Any]


class EventBus:
    """Simple synchronous event bus with wildcard support.

    Usage::

        bus = EventBus()
        bus.on("jarvis.state_changed", lambda data: print(data))
        bus.on("device.*", lambda data: print("device event", data))
        bus.emit("jarvis.state_changed", {"state": "listening"})
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}
        self._lock = threading.Lock()

    def on(self, event: str, callback: Listener) -> None:
        """Subscribe to an event. Supports wildcards like ``device.*``."""
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Listener) -> None:
        """Unsubscribe a callback from an event."""
        with self._lock:
            listeners = self._listeners.get(event, [])
            try:
                listeners.remove(callback)
            except ValueError:
                pass

    def emit(self, event: str, data: Any = None) -> None:
        """Emit an event, notifying all matching listeners.

        Exceptions in listeners are logged but do not interrupt other
        listeners or the caller.
        """
        with self._lock:
            matched: list[Listener] = []
            for pattern, listeners in self._listeners.items():
                if pattern == event or fnmatch.fnmatch(event, pattern):
                    matched.extend(listeners)

        for callback in matched:
            try:
                callback(data)
            except Exception:
                LOGGER.exception(
                    "Listener %s failed on event %s", callback, event,
                )

    def clear(self) -> None:
        """Remove all listeners."""
        with self._lock:
            self._listeners.clear()
