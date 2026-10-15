"""ADR 0272: how long a spoken turn's answer waits for the end of his thought.

A pause in his speech is not always the end of it. The surface that heard him ends the
turn after a short silence, and the rest of the thought sometimes comes a second later.
The model request goes out at once; what leaves her (the answer's sentences, its audio,
a write) waits for this hold, so that the rest, arriving within it, finds nothing of the
first answer out and takes its place. A short line that reads as finished gets no hold.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.pre_route import spoken_turn
from jarvis.surface import voice_asr

if TYPE_CHECKING:
    from jarvis.shared import Event

DEFAULT_HOLD_S: Final = 1.5
"""Seconds from the end of his speech that a spoken answer waits."""
DEFAULT_SHORT_CHARS: Final = 10
"""A line of at most this many characters that reads as finished gets no hold."""


def _setting(config: Mapping[str, Any], key: str, default: float) -> float:
    realtime = config.get("realtime")
    response = realtime.get("response") if isinstance(realtime, Mapping) else None
    block = response.get("answer_hold") if isinstance(response, Mapping) else None
    value = block.get(key) if isinstance(block, Mapping) else None
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value >= 0
    return float(value) if ok else default  # type: ignore[arg-type]


def hold_seconds(config: Mapping[str, Any], trigger: Event) -> float:
    """Seconds from the end of his speech that the answer to ``trigger`` waits; 0 for none."""
    if not spoken_turn(trigger):
        return 0.0
    words = str(trigger.payload.get("transcript", "")).strip()
    short = _setting(config, "short_chars", DEFAULT_SHORT_CHARS)
    if len(words) <= short and voice_asr.looks_complete(words):
        return 0.0
    return _setting(config, "seconds", DEFAULT_HOLD_S)


def output_not_before(config: Mapping[str, Any], trigger: Event) -> float | None:
    """The monotonic time before which the answer to ``trigger`` does not leave her.

    Counted from the end of his speech: the event's own time, less the ``spoke_ago_ms`` the
    device that heard him wrote when its capture ended earlier than the event (the Mac's
    endpoint wait and final ASR). The phone's ``say`` has no such field: it arrives at the
    end of its own wait, and the hold counts from there. ``None`` when there is no hold, or
    it has already passed.
    """
    hold = hold_seconds(config, trigger)
    if hold <= 0:
        return None
    spoke_ago = trigger.payload.get("spoke_ago_ms")
    ago_s = spoke_ago / 1000 if isinstance(spoke_ago, int | float) else 0.0
    left = trigger.ts_epoch_ms / 1000 - ago_s + hold - time.time()
    return time.monotonic() + left if left > 0 else None
