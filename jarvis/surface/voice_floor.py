"""ADR 0407: one device answers his voice at a time, named by the brain before he speaks.

Every device that listens tells the brain three things as they change: whether it is in a
conversation (``busy``: listening, thinking, speaking or waiting for a follow-up), whether it
can hear his wake word now (``hears``), and that he just used it (a touch on a phone's screen,
the Mac's keyboard or mouse). The brain keeps one answer from them, the device that has the
floor:

* the device that most recently began a conversation, for as long as it is in it;
* otherwise the device he used last, of those that can hear the wake word.

Each device is told where it stands, relative to itself: ``mine`` (it answers the next wake
word; also when the brain names no device), ``other`` (another device he used later answers it)
or ``taken`` (another device is in a conversation). A device decides at the wake word from what
it was last told, so arbitration adds no wait. Words from a device whose floor is ``taken`` are
no turn: the brain judges them ``elsewhere`` (:mod:`jarvis.surface.word_judge`).

Layer rules: stdlib only.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger("jarvis.surface.voice_floor")

type Standing = Literal["mine", "other", "taken"]
STANDINGS: Final = frozenset({"mine", "other", "taken"})


class VoiceFloor:
    """The brain's one answer to "which device answers him now"; any thread may call it."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """No device yet: every device's standing is ``mine``."""
        self._clock = clock
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._hears: set[str] = set()
        self._used: dict[str, float] = {}
        self._began: str | None = None  # the device that most recently began a conversation
        self.on_change: Callable[[], None] | None = None
        """Called, outside the lock, when the floor moves or its conversation begins or ends."""

    def report(
        self, device: str, *, busy: bool | None = None, hears: bool | None = None,
        used_ago_s: float | None = None,
    ) -> None:
        """What ``device`` says of itself; ``None`` leaves that part as it was.

        ``used_ago_s`` is how long ago he last used it, by its own count (0 for a touch now).
        """
        with self._lock:
            before = self._seen()
            if busy is True and device not in self._busy:
                self._busy.add(device)
                self._began = device
            elif busy is False:
                self._busy.discard(device)
            if hears is True:
                self._hears.add(device)
            elif hears is False:
                self._hears.discard(device)
            if used_ago_s is not None and math.isfinite(used_ago_s):
                at = self._clock() - max(0.0, used_ago_s)
                self._used[device] = max(at, self._used.get(device, -math.inf))
            after = self._seen()
        self._changed(before, after)

    def left(self, device: str) -> None:
        """``device`` went away: it neither hears nor is in a conversation."""
        self.report(device, busy=False, hears=False)

    def standing(self, device: str) -> Standing:
        """Where ``device`` stands: ``mine``, ``other`` or ``taken``."""
        with self._lock:
            holder, busy = self._seen()
        if holder is None or holder == device:
            return "mine"
        return "taken" if busy else "other"

    def taken_from(self, device: str) -> bool:
        """Whether another device is in a conversation, so words ``device`` heard are no turn."""
        return self.standing(device) == "taken"

    def _seen(self) -> tuple[str | None, bool]:
        """``(holder, in a conversation)``; call under the lock."""
        if self._began is not None and self._began in self._busy:
            return self._began, True
        hearing = sorted(self._hears)  # by name, so a tie always goes the same way
        if hearing:
            return max(hearing, key=lambda d: self._used.get(d, -math.inf)), False
        return None, False

    def _changed(self, before: tuple[str | None, bool], after: tuple[str | None, bool]) -> None:
        if after == before:
            return
        holder, busy = after
        LOGGER.info("voice floor: %s%s", holder or "nobody", " (in a conversation)" if busy else "")
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # telling the devices cannot break the report
                LOGGER.exception("voice floor: telling the devices failed")
