"""ADR-0005 §4.2 — the assistant's output lease.

The media actor holds one lease across provider I/O and queued playback of an
answer. The night run (``night.busy``) waits while any lease is held, so it
never mutes the Mac under a spoken line. Nothing mutes the system output any
more: the wake capture that did, and its refcounted osascript duck/restore,
are gone.

Layer placement: L5 surface. Only stdlib imports.
"""

from __future__ import annotations

import threading


class SystemAudioDucker:
    """Counts the output leases currently held."""

    def __init__(self) -> None:
        """Start with no lease held."""
        self._lock = threading.Lock()
        self._output_depth = 0

    @property
    def outputting(self) -> bool:
        """Return True while an output lease is held: an answer or media is being made or played."""
        with self._lock:
            return self._output_depth > 0

    def enter_output(self) -> None:
        """Acquire one output lease."""
        with self._lock:
            self._output_depth += 1

    def leave_output(self) -> None:
        """Release one output lease acquired by :meth:`enter_output`."""
        with self._lock:
            if self._output_depth <= 0:
                msg = "leave_output called without an active output lease"
                raise RuntimeError(msg)
            self._output_depth -= 1
