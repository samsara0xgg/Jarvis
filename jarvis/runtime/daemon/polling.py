"""How often the daemon's watchers poll the event log, and how long a failed poll waits."""

from __future__ import annotations

# Default poll cadence for both watchers; 10 ms balances CPU against
# first-byte latency between an L2 INSERT (Step 7 submit handler / L3
# render emission) and the watcher's reaction.
DEFAULT_POLL_INTERVAL_S: float = 0.01

# How long a daemon loop waits after a failed poll before it tries again; long
# enough that a persistent error writes one log line a second, not a hundred.
WATCHER_RETRY_S: float = 1.0
