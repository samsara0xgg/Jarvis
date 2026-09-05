"""In-process notifications for events whose outer transaction committed."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from jarvis.shared import Event

LOGGER = logging.getLogger(__name__)

CommittedEventSubscriber = Callable[[Event], None]


class CommittedEventBus:
    """Small thread-safe fan-out used only after successful outer COMMIT.

    Subscriber failures are isolated: the durable Event Log remains the
    recovery source, so one broken direct listener must not turn a committed
    append into an apparent transaction failure for its caller.
    """

    def __init__(self) -> None:
        """Create an empty subscriber set."""
        self._lock = threading.Lock()
        self._subscribers: list[CommittedEventSubscriber] = []

    def subscribe(self, subscriber: CommittedEventSubscriber) -> Callable[[], None]:
        """Register ``subscriber`` and return an idempotent unsubscribe."""
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(subscriber)
                except ValueError:
                    return

        return _unsubscribe

    def publish(self, event: Event) -> None:
        """Notify a stable subscriber snapshot about one committed event."""
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber(event)
            except Exception:  # pragma: no cover - recovery is the durable watcher
                LOGGER.exception(
                    "committed-event subscriber failed for event_uid=%s",
                    event.event_uid,
                )


__all__ = ["CommittedEventBus", "CommittedEventSubscriber"]
