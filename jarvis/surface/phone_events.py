"""ADR 0197: the events a paired phone reports, and the strict check of what each one carries.

A phone wakes for a few seconds at a time, so it sends what it sensed as one batch of event frames
in a single ``POST /inherent/device/events`` (the route of :mod:`jarvis.surface.inherent_server`)
under its device token. A frame has the shape of the ``/terminal/ws`` event frame
(:mod:`jarvis.surface.terminal_events`) and nothing more::

    {"event_uid": "<32 hex>", "event_type": "phone.location_observed",
     "ts_epoch_ms": 1700000000000, "payload": {...}}

The brain appends a frame through :meth:`BrainEvents.record_phone_batch`, which holds the
dedupe, the clock clamp and the append; this module holds what is the phone's own: the five
types, the fields and ranges of their payloads, and the shape of a frame. The registry in
:mod:`jarvis.state.event_log` holds which fields each type requires.

Every refusal is ``bad_event``: an unknown key, a wrong type (a bool is not a number), a number
that is not finite or is out of range, a value outside its enum, a string over its cap, a window
that ends before it starts, or a time later than the brain's clock by more than five minutes.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from jarvis.state.day_line import HEALTH_EVENT, MOTION_EVENT
from jarvis.state.event_log import EventTypeRegistry
from jarvis.state.phone_location import LOCATION_EVENT, VISIT_EVENT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    Check = Callable[[object, int], bool]

PHONE_EVENTS_PATH: Final = "/inherent/device/events"

STATE_EVENT: Final = "phone.state_observed"

PHONE_EVENT_TYPES: Final = frozenset(
    {VISIT_EVENT, LOCATION_EVENT, MOTION_EVENT, HEALTH_EVENT, STATE_EVENT},
)
"""Every type a phone may write, and nothing else: no confirmation, no utterance, no tool
result, and none of the types a terminal sends over its link."""

MAX_BATCH_FRAMES: Final = 200
MAX_BATCH_BYTES: Final = 1024 * 1024
MAX_PLACE_CHARS: Final = 200
MAX_DETAIL_CHARS: Final = 64

ACTIVITIES: Final = ("stationary", "walking", "running", "cycling", "automotive", "unknown")
CONFIDENCES: Final = ("low", "medium", "high")
METRICS: Final = (
    "steps", "distance_m", "active_kcal", "exercise_min", "sleep_min", "workout_min",
)
SIGNAL_VALUES: Final[Mapping[str, tuple[str, ...]]] = {
    "focus": ("on", "off"),
    "alarm": ("stopped", "snoozed"),
    "power": ("connected", "disconnected"),
    "headphones": ("connected", "disconnected"),
    "call": ("started", "ended"),
}

_FUTURE_MS: Final = 5 * 60 * 1000
_FRAME_KEYS: Final = frozenset({"type", "event_uid", "event_type", "ts_epoch_ms", "payload"})


def _number(value: object) -> float | None:
    """``value`` as a finite number, or ``None`` for a bool, a non-number, NaN or infinity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value  # any size is finite; the range checks compare it as it is
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _within(low: float, high: float) -> Check:
    def check(value: object, _now_ms: int) -> bool:
        number = _number(value)
        return number is not None and low <= number <= high

    return check


def _non_negative(value: object, _now_ms: int) -> bool:
    number = _number(value)
    return number is not None and number >= 0


def _time(value: object, now_ms: int) -> bool:
    """Epoch milliseconds as an integer, no later than the brain's clock plus five minutes."""
    return type(value) is int and 0 <= value <= now_ms + _FUTURE_MS


def _text(limit: int) -> Check:
    def check(value: object, _now_ms: int) -> bool:
        return isinstance(value, str) and len(value) <= limit

    return check


def _one_of(options: tuple[str, ...]) -> Check:
    def check(value: object, _now_ms: int) -> bool:
        return isinstance(value, str) and value in options

    return check


_LAT: Final = _within(-90, 90)
_LNG: Final = _within(-180, 180)
_PLACE: Final = _text(MAX_PLACE_CHARS)
_DETAIL: Final = _text(MAX_DETAIL_CHARS)

_CHECKS: Final[Mapping[str, Mapping[str, Check]]] = {
    VISIT_EVENT: {
        "lat": _LAT, "lng": _LNG, "accuracy_m": _non_negative, "arrived_at_ms": _time,
        "departed_at_ms": _time, "place": _PLACE,
    },
    LOCATION_EVENT: {
        "lat": _LAT, "lng": _LNG, "accuracy_m": _non_negative, "place": _PLACE,
        "speed_mps": _non_negative,
    },
    MOTION_EVENT: {
        "activity": _one_of(ACTIVITIES), "confidence": _one_of(CONFIDENCES),
        "started_at_ms": _time, "ended_at_ms": _time,
    },
    HEALTH_EVENT: {
        "metric": _one_of(METRICS), "start_ms": _time, "end_ms": _time,
        "value": _non_negative, "detail": _DETAIL,
    },
    STATE_EVENT: {
        "signal": _one_of(tuple(SIGNAL_VALUES)),
        "value": _one_of(tuple({v for values in SIGNAL_VALUES.values() for v in values})),
        "detail": _DETAIL,
    },
}
"""The check of each field a phone type may carry; a key outside a type's checks is unknown."""

_WINDOWS: Final[Mapping[str, tuple[str, str]]] = {
    VISIT_EVENT: ("arrived_at_ms", "departed_at_ms"),
    MOTION_EVENT: ("started_at_ms", "ended_at_ms"),
    HEALTH_EVENT: ("start_ms", "end_ms"),
}
"""The (start, end) fields of the types that cover a span; the end may not precede the start."""


def payload_valid(event_type: str, payload: Mapping[str, object], now_ms: int) -> bool:
    """Whether ``payload`` is exactly what a phone's ``event_type`` event carries.

    ``now_ms`` is the brain's clock: no time in a payload may be later than it by more than
    five minutes. A type that is not a phone type is not valid here.
    """
    checks, schema = _CHECKS.get(event_type), EventTypeRegistry.get(event_type)
    if checks is None or schema is None:
        return False
    if not set(schema.required_payload) <= payload.keys():
        return False
    if not all(key in checks and checks[key](value, now_ms) for key, value in payload.items()):
        return False
    window = _WINDOWS.get(event_type)
    if window is not None and window[1] in payload:
        start, end = payload[window[0]], payload[window[1]]
        if not (isinstance(start, int) and isinstance(end, int) and end >= start):
            return False
    if event_type == STATE_EVENT:
        return payload["value"] in SIGNAL_VALUES[str(payload["signal"])]
    return True


def frame_shaped(frame: Mapping[str, object]) -> bool:
    """Whether a phone's frame carries only the keys of the event frame it is shaped like.

    A phone does not link its events to rows of the log (``source_event_id``,
    ``correlation``) or pick a schema version; ``type``, if sent, says ``event``.
    """
    return frame.keys() <= _FRAME_KEYS and frame.get("type", "event") == "event"
