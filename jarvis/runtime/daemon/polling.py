"""How often the daemon's watchers poll the event log, and how long a failed poll waits."""

from __future__ import annotations

from jarvis.runtime.turn.wait import DEFAULT_POLL_INTERVAL_S

__all__ = ["DEFAULT_POLL_INTERVAL_S", "WATCHER_RETRY_S"]

# How long a daemon loop waits after a failed poll before it tries again; long
# enough that a persistent error writes one log line a second, not a hundred.
WATCHER_RETRY_S: float = 1.0
