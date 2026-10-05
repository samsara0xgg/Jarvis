"""The daemon's one shared decision-snapshot cache (ADR 0164).

L2 offers an immutable fold state and a reader that advances it; the holder
lives here because only the runtime wires one reader into every turn thread.
Turns run on worker threads with connections of their own, so the state is
published by swapping one reference under a lock: a state is never mutated
once shared, and a thread that loses a race just folds a few events twice.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, Final

from jarvis.state.decision_snapshot import (
    DecisionFoldState,
    DecisionStateSnapshot,
    read_decision_snapshot,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

LOGGER = logging.getLogger("jarvis.runtime.decision_state")

# One read in this many is checked against a whole-log fold off the voice path.
# Every turn reads at least twice, so this is a check every few dozen turns; the
# whole-log fold costs about as much CPU as one old read.
_VERIFY_EVERY_READS: Final[int] = 100
_IDLE_POLL_S: Final[float] = 5.0


class DecisionStateCache:
    """Hands every turn the decision snapshot, folding only what was appended since the last."""

    def __init__(
        self,
        open_connection: Callable[[], sqlite3.Connection],
        *,
        verify_every_reads: int = _VERIFY_EVERY_READS,
        is_idle: Callable[[], bool] | None = None,
        idle_poll_s: float = _IDLE_POLL_S,
    ) -> None:
        """``open_connection`` opens a fresh read connection for the background self-check.

        ``is_idle`` says no turn is in flight and none has just spoken; a due
        self-check waits for it, polling every ``idle_poll_s``. ``None`` never waits.
        """
        self._open = open_connection
        self._is_idle = is_idle
        self._idle_poll_s = idle_poll_s
        self._verify_every = verify_every_reads
        self._lock = threading.Lock()
        self._state: DecisionFoldState | None = None
        self._reads = 0
        self._verifying = False

    @property
    def cursor(self) -> int | None:
        """The event id the shared state is folded up to, or None before the first read."""
        with self._lock:
            return None if self._state is None else self._state.cursor

    def read(self, conn: sqlite3.Connection) -> DecisionStateSnapshot:
        """Read the snapshot on ``conn``'s view, advancing the shared state when it is newer."""
        with self._lock:
            prior = self._state
            self._reads += 1
            due = self._verify_every > 0 and self._reads % self._verify_every == 0
        snapshot = read_decision_snapshot(conn, prior)
        fresh = snapshot.fold_state
        if fresh is not None:
            with self._lock:
                held = self._state
                if held is None or fresh.cursor > held.cursor:
                    self._state = fresh
        if due:
            self.verify_in_background()
        return snapshot

    def warm(self) -> None:
        """Fold the whole log once in the background, so the first turn reads only a delta."""

        def _run() -> None:
            try:
                with contextlib.closing(self._open()) as conn:
                    self.read(conn)
            except Exception:  # noqa: BLE001 - a failed warm-up costs only the first turn's read
                LOGGER.warning("decision state warm-up failed", exc_info=True)

        threading.Thread(target=_run, name="jarvis-decision-state-warm", daemon=True).start()

    def reset(self) -> None:
        """Forget the shared state; the next read folds the whole log."""
        with self._lock:
            self._state = None

    def verify_in_background(self) -> None:
        """Compare the incremental state with a whole-log fold on a thread of its own, once idle."""
        with self._lock:
            if self._verifying:
                return
            self._verifying = True
        thread = threading.Thread(
            target=self._verify, name="jarvis-decision-state-check", daemon=True,
        )
        thread.start()

    def _verify(self) -> None:
        try:
            # Never while a turn works or speaks: the comparison is a whole-log fold of CPU.
            while self._is_idle is not None and not self._is_idle():
                time.sleep(self._idle_poll_s)
            if not self.agrees_with_whole_log():
                LOGGER.warning("incremental decision state differs from a whole-log fold; reset")
                self.reset()
        except Exception:  # noqa: BLE001 - the check must never take a turn down
            LOGGER.warning("decision state self-check failed", exc_info=True)
        finally:
            with self._lock:
                self._verifying = False

    def agrees_with_whole_log(self) -> bool:
        """Whether the shared state, advanced to a view, equals a whole-log fold of that view."""
        with self._lock:
            prior = self._state
        if prior is None:
            return True
        with contextlib.closing(self._open()) as conn:
            conn.execute("BEGIN")
            try:
                warm = read_decision_snapshot(conn, prior)  # same view, so the same cursor
                cold = read_decision_snapshot(conn)
            finally:
                conn.rollback()
        return (
            warm.event_cursor == cold.event_cursor
            and warm.projections == cold.projections
            and warm.authorizations == cold.authorizations
        )
