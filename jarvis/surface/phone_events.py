"""ADR 0197: the events a paired phone reports, and the strict check of what each one carries.

A phone wakes for a few seconds at a time, so it sends what it sensed as one batch of event frames
in a single ``POST /inherent/device/events`` (the route of
:mod:`jarvis.surface.inherent_server.phone`) under its device token. A frame has the shape of the
``/terminal/ws`` event frame (:mod:`jarvis.surface.terminal_events`) and nothing more::

    {"event_uid": "<32 hex>", "event_type": "phone.location_observed",
     "ts_epoch_ms": 1700000000000, "payload": {...}}

The brain appends a frame through :meth:`BrainEvents.record_phone_batch`, which holds the
dedupe, the clock clamp and the append; this module holds what is the phone's own: the seven
types, the fields and ranges of their payloads, and the shape of a frame. The registry in
:mod:`jarvis.state.event_log` holds which fields each type requires.

Every refusal is ``bad_event``: an unknown key, a wrong type (a bool is not a number), a number
that is not finite or is out of range, a value outside its enum, a string over its cap, a window
that ends before it starts, or a time later than the brain's clock by more than five minutes.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Final

from jarvis.state.attachments import valid_id
from jarvis.state.event_log import EventTypeRegistry
from jarvis.state.locate_wait import valid_request_id
from jarvis.state.phone_day import HEALTH_EVENT, MEASURE_EVENT, MOTION_EVENT
from jarvis.state.phone_location import LOCATION_EVENT, VISIT_EVENT
from jarvis.state.phone_track import FIX_COLUMNS, FROM_ACCESSORY, SIMULATED, TRACK_EVENT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    Check = Callable[[object, int], bool]

PHONE_EVENTS_PATH: Final = "/inherent/device/events"

STATE_EVENT: Final = "phone.state_observed"

PHONE_EVENT_TYPES: Final = frozenset(
    {
        VISIT_EVENT, LOCATION_EVENT, MOTION_EVENT, HEALTH_EVENT, STATE_EVENT, MEASURE_EVENT,
        TRACK_EVENT,
    },
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
    # hourly totals of cumulative quantities (Health merges iPhone and Watch only in statistics)
    "steps", "active_energy", "basal_energy", "exercise_time", "stand_time",
    "distance_walking_running", "flights_climbed", "time_in_daylight",
    # spans as the source recorded them: one event per Health sample
    "sleep", "workout", "mindful_session", "stand_hour",
)
SLEEP_STAGES: Final = ("in_bed", "awake", "unspecified", "core", "deep", "rem")
STAND_STATES: Final = ("stood", "idle")
MEASURES: Final = (
    "heart_rate", "resting_heart_rate", "walking_heart_rate_average",
    "heart_rate_variability_sdnn", "heartbeat_series", "oxygen_saturation", "respiratory_rate",
    "sleeping_wrist_temperature", "vo2_max", "environmental_audio_exposure",
    "headphone_audio_exposure",
)
HEARTBEAT_SERIES: Final = "heartbeat_series"
MAX_BEATS: Final = 4000
"""Beats one event carries: [offset_ms, gap] is about ten characters, and a frame stays under
64 KiB. A longer series is sent as several events, told apart by ``part``."""
MAX_PARTS: Final = 1000
MAX_TRACK_FIXES: Final = 300
"""Fixes one track frame carries: a row is about a hundred characters, and a frame stays under
64 KiB. The phone sends a minute at a time, about 60 at one fix a second."""
MAX_TRACK_SPAN_MS: Final = 10 * 60 * 1000
MAX_UNIT_CHARS: Final = 16
MAX_SOURCE_CHARS: Final = 128
MAX_META_KEYS: Final = 12
MAX_META_CHARS: Final = 128
SIGNAL_VALUES: Final[Mapping[str, tuple[str, ...]]] = {
    "focus": ("on", "off"),
    "alarm": ("stopped", "snoozed"),
    "power": ("connected", "disconnected"),
    "headphones": ("connected", "disconnected"),
    "call": ("started", "ended"),
}
BATTERY_SIGNAL: Final = "battery"
"""The one signal whose ``value`` is not an enum: the charge as a whole percent, in text."""
BATTERY_DETAILS: Final = ("charging", "full", "unplugged")

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


def _finite(value: object, _now_ms: int) -> bool:
    return _number(value) is not None


_UUID: Final = re.compile(r"[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_META_KEY: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _uuid(value: object, _now_ms: int) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _meta(value: object, _now_ms: int) -> bool:
    """A small object of the source's own words: text, finite numbers and booleans by name."""
    if not isinstance(value, dict) or len(value) > MAX_META_KEYS:
        return False
    return all(
        isinstance(key, str) and _META_KEY.fullmatch(key) is not None
        and (isinstance(item, bool) or _number(item) is not None
             or (isinstance(item, str) and len(item) <= MAX_META_CHARS))
        for key, item in value.items()
    )


def _beats(value: object, _now_ms: int) -> bool:
    """``[[offset_ms, preceded_by_gap], ...]``: offsets from the series start, gap 0 or 1."""
    return (
        isinstance(value, list) and 0 < len(value) <= MAX_BEATS
        and all(
            isinstance(beat, list) and len(beat) == 2  # noqa: PLR2004 — a pair
            and type(beat[0]) is int and beat[0] >= 0 and type(beat[1]) is int and beat[1] in (0, 1)
            for beat in value
        )
    )


def _nullable(check: Check) -> Check:
    def nullable(value: object, now_ms: int) -> bool:
        return value is None or check(value, now_ms)

    return nullable


_FLAGS: Final = SIMULATED | FROM_ACCESSORY


def _offset(value: object, _now_ms: int) -> bool:
    return type(value) is int and value >= 0


def _course(value: object, _now_ms: int) -> bool:
    """Degrees clockwise from true north, 0 up to but not 360."""
    number = _number(value)
    return number is not None and 0 <= number < 360  # noqa: PLR2004 — a full turn


def _flags(value: object, _now_ms: int) -> bool:
    return type(value) is int and 0 <= value <= _FLAGS


def _fix_checks() -> tuple[Check, ...]:
    """One check per column of :data:`FIX_COLUMNS`, in order."""
    return (
        _offset, _within(-90, 90), _within(-180, 180), _non_negative,
        _nullable(_finite), _nullable(_non_negative),
        _nullable(_non_negative), _nullable(_non_negative),
        _nullable(_course),
        _nullable(_non_negative), _flags,
    )


_FIX: Final = _fix_checks()


def _fixes(value: object, now_ms: int) -> bool:
    """Rows of :data:`FIX_COLUMNS`, at most :data:`MAX_TRACK_FIXES`, in time order."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_TRACK_FIXES:
        return False
    last = 0
    for fix in value:
        if not (
            isinstance(fix, list) and len(fix) == len(FIX_COLUMNS)
            and all(check(item, now_ms) for check, item in zip(_FIX, fix, strict=True))
            and fix[0] >= last
        ):
            return False
        last = fix[0]
    return True


def _part(value: object, _now_ms: int) -> bool:
    return type(value) is int and 0 <= value < MAX_PARTS


_LAT: Final = _within(-90, 90)
_LNG: Final = _within(-180, 180)
_PLACE: Final = _text(MAX_PLACE_CHARS)
_DETAIL: Final = _text(MAX_DETAIL_CHARS)
_UNIT: Final = _text(MAX_UNIT_CHARS)
_SOURCE: Final = _text(MAX_SOURCE_CHARS)

_CHECKS: Final[Mapping[str, Mapping[str, Check]]] = {
    VISIT_EVENT: {
        "lat": _LAT, "lng": _LNG, "accuracy_m": _non_negative, "arrived_at_ms": _time,
        "departed_at_ms": _time, "place": _PLACE,
    },
    LOCATION_EVENT: {
        "lat": _LAT, "lng": _LNG, "accuracy_m": _non_negative, "place": _PLACE,
        "speed_mps": _non_negative, "request_id": lambda value, _now_ms: valid_request_id(value),
        "snapshot": lambda value, _now_ms: valid_id(value),
    },
    MOTION_EVENT: {
        "activity": _one_of(ACTIVITIES), "confidence": _one_of(CONFIDENCES),
        "started_at_ms": _time, "ended_at_ms": _time,
    },
    HEALTH_EVENT: {
        "metric": _one_of(METRICS), "start_ms": _time, "end_ms": _time,
        "value": _non_negative, "detail": _DETAIL, "unit": _UNIT, "source": _SOURCE,
        "device": _SOURCE, "sample_uuid": _uuid, "meta": _meta,
    },
    MEASURE_EVENT: {
        "metric": _one_of(MEASURES), "start_ms": _time, "end_ms": _time, "value": _finite,
        "unit": _UNIT, "sample_uuid": _uuid, "source": _SOURCE, "device": _SOURCE,
        "meta": _meta, "beats": _beats, "part": _part,
    },
    TRACK_EVENT: {"started_at_ms": _time, "ended_at_ms": _time, "fixes": _fixes},
    STATE_EVENT: {
        "signal": _one_of((*SIGNAL_VALUES, BATTERY_SIGNAL)),
        "value": _DETAIL,  # which texts fit which signal is checked in payload_valid
        "detail": _DETAIL,
    },
}
"""The check of each field a phone type may carry; a key outside a type's checks is unknown."""

_WINDOWS: Final[Mapping[str, tuple[str, str]]] = {
    VISIT_EVENT: ("arrived_at_ms", "departed_at_ms"),
    MOTION_EVENT: ("started_at_ms", "ended_at_ms"),
    HEALTH_EVENT: ("start_ms", "end_ms"),
    MEASURE_EVENT: ("start_ms", "end_ms"),
    TRACK_EVENT: ("started_at_ms", "ended_at_ms"),
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
    rule = _RULES.get(event_type)
    return rule is None or rule(payload)


def _health_valid(payload: Mapping[str, object]) -> bool:
    """A sleep span names its stage and a stand hour its state; the rest need no detail."""
    metric, detail = payload["metric"], payload.get("detail")
    if metric == "sleep":
        return detail in SLEEP_STAGES
    if metric == "stand_hour":
        return detail in STAND_STATES
    return True


def _measure_valid(payload: Mapping[str, object]) -> bool:
    """A heartbeat series carries its beats (``value`` counts them); no other measure does."""
    beats = payload.get("beats")
    if payload["metric"] != HEARTBEAT_SERIES:
        return beats is None and "part" not in payload
    return isinstance(beats, list) and payload["value"] == len(beats)


def _state_valid(payload: Mapping[str, object]) -> bool:
    """Whether ``value`` (and ``detail``) is what ``signal`` carries."""
    signal, value, detail = str(payload["signal"]), payload["value"], payload.get("detail")
    if signal == BATTERY_SIGNAL:
        return (
            isinstance(value, str)
            and value.isascii()
            and value.isdigit()
            and str(int(value)) == value  # no leading zeros
            and int(value) <= 100  # noqa: PLR2004 — a percent
            and (detail is None or detail in BATTERY_DETAILS)
        )
    return value in SIGNAL_VALUES[signal]


def _track_valid(payload: Mapping[str, object]) -> bool:
    """The first fix is at the start, the last at the end, and the span is ten minutes at most."""
    start, end, fixes = payload["started_at_ms"], payload["ended_at_ms"], payload["fixes"]
    return (
        isinstance(start, int) and isinstance(end, int) and isinstance(fixes, list)
        and end - start <= MAX_TRACK_SPAN_MS and fixes[0][0] == 0 and fixes[-1][0] == end - start
    )


_RULES: Final[Mapping[str, Callable[[Mapping[str, object]], bool]]] = {
    HEALTH_EVENT: _health_valid, MEASURE_EVENT: _measure_valid, STATE_EVENT: _state_valid,
    TRACK_EVENT: _track_valid,
}
"""What a type's fields must also satisfy together, beyond each field's own check."""


def frame_shaped(frame: Mapping[str, object]) -> bool:
    """Whether a phone's frame carries only the keys of the event frame it is shaped like.

    A phone does not link its events to rows of the log (``source_event_id``,
    ``correlation``) or pick a schema version; ``type``, if sent, says ``event``.
    """
    return frame.keys() <= _FRAME_KEYS and frame.get("type", "event") == "event"
