"""Bounded, process-local monotonic telemetry for realtime latency work.

Trace points are diagnostic observations, not canonical Event Log facts.  The
sink deliberately stores no response/user text and never performs file or
database I/O.  Callers may snapshot it for an integration burn, then reset it
between runs.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

type TraceValue = str | int | float | bool | None

_MAX_TRACE_POINTS: Final[int] = 4096
_TRACE_LOCK = threading.Lock()
_TRACE_POINTS: deque[RealtimeTracePoint]


@dataclass(frozen=True)
class RealtimeTracePoint:
    """One non-authoritative observation on the process monotonic clock."""

    name: str
    monotonic_ns: int
    attributes: Mapping[str, TraceValue]
    telemetry_only: bool = True


_TRACE_POINTS = deque(maxlen=_MAX_TRACE_POINTS)


def record_realtime_trace(name: str, **attributes: TraceValue) -> RealtimeTracePoint:
    """Append one trace point to the bounded in-memory sink."""
    if not name:
        msg = "realtime trace name must be non-empty"
        raise ValueError(msg)
    frozen_attributes = MappingProxyType(dict(attributes))
    with _TRACE_LOCK:
        # Timestamp and append share the lock so concurrent producers cannot
        # expose points in reverse monotonic order to a snapshot consumer.
        point = RealtimeTracePoint(
            name=name,
            monotonic_ns=time.monotonic_ns(),
            attributes=frozen_attributes,
        )
        _TRACE_POINTS.append(point)
    return point


def realtime_trace_snapshot() -> tuple[RealtimeTracePoint, ...]:
    """Return an immutable point-in-time copy in append order."""
    with _TRACE_LOCK:
        return tuple(_TRACE_POINTS)


def reset_realtime_trace() -> None:
    """Clear diagnostic state without touching canonical runtime state."""
    with _TRACE_LOCK:
        _TRACE_POINTS.clear()


__all__ = [
    "RealtimeTracePoint",
    "TraceValue",
    "realtime_trace_snapshot",
    "record_realtime_trace",
    "reset_realtime_trace",
]
