"""Push how much of her answer has really been heard to the connected surfaces.

The playback ledger already knows the conservative heard text of the answer
playing now (``OutputTimelineSnapshot.heard_text``); this only forwards it, as
the ``playback`` op, so a caption can follow the audio instead of a timer
(ADR 0112). It adds no audio logic: a held answer does not move its cursor, so
nothing is sent while she is held, and the caption stops with her.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# About 16 messages a second at most; the presentation poll runs every 5 ms, so a
# change skipped here is offered again on the next poll.
MIN_INTERVAL_S = 0.06


class HeardFeed:
    """Throttled, change-only sender of the heard text of the playing answer."""

    def __init__(
        self,
        broadcaster: object | None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the broadcaster's worker-thread op bridge, if it has one."""
        self._send = getattr(broadcaster, "broadcast_op_sync", None)
        self._clock = clock
        self._last: tuple[str, int, str] | None = None
        self._last_at = float("-inf")

    def offer(
        self,
        *,
        turn_id: str,
        response_id: str,
        playback_generation_id: int,
        heard: str,
        final: bool = False,
    ) -> None:
        """Send ``heard`` if it changed; ``final`` (the terminal snapshot) skips the throttle."""
        if not callable(self._send) or not heard:
            return
        key = (response_id, playback_generation_id, heard)
        now = self._clock()
        if key == self._last or (not final and now - self._last_at < MIN_INTERVAL_S):
            return
        self._last, self._last_at = key, now
        with contextlib.suppress(Exception):
            self._send("playback", turn_id=turn_id, response_id=response_id, heard=heard)
