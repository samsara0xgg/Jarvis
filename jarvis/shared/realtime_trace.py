"""Bounded monotonic telemetry for realtime latency work.

Trace points are diagnostic observations, not canonical Event Log facts.  The
default sink is process-local and stores no response/user text.  A controlled
JSONL exporter can be enabled explicitly for live burns; every exported row is
marked ``telemetry_only`` and ``production_fact=false`` so it cannot be
mistaken for Event Log truth.
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

type TraceValue = str | int | float | bool | None

_MAX_TRACE_POINTS: Final[int] = 4096
_TRACE_LOCK = threading.Lock()
_TRACE_POINTS: deque[RealtimeTracePoint]
_TRACE_CONTEXT: contextvars.ContextVar[Mapping[str, TraceValue]] = contextvars.ContextVar(
    "jarvis_realtime_trace_context",
    default=MappingProxyType({}),
)
_EXPORT_LOCK = threading.Lock()
_EXPORTER: _JsonlExporter | None
_EXPORT_PATH: Path | None = None

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RealtimeTracePoint:
    """One non-authoritative observation on the process monotonic clock."""

    name: str
    monotonic_ns: int
    attributes: Mapping[str, TraceValue]
    telemetry_only: bool = True


_TRACE_POINTS = deque(maxlen=_MAX_TRACE_POINTS)
_EXPORTER = None


class _JsonlExporter:
    """One bounded background writer so producers never perform disk I/O."""

    def __init__(self, path: Path) -> None:
        self._fd = os.open(
            path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        self._queue: queue.Queue[RealtimeTracePoint | None] = queue.Queue(
            maxsize=_MAX_TRACE_POINTS,
        )
        self._thread = threading.Thread(
            target=self._run,
            name="jarvis-realtime-trace-export",
            daemon=True,
        )
        self._thread.start()

    def submit(self, point: RealtimeTracePoint) -> None:
        try:
            self._queue.put_nowait(point)
        except queue.Full:
            LOGGER.warning("realtime trace JSONL queue full; dropping point")

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # Make room for the shutdown sentinel without blocking a caller.
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            self._queue.put_nowait(None)
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            LOGGER.warning("realtime trace JSONL writer did not stop within 2s")

    def _run(self) -> None:
        try:
            while True:
                point = self._queue.get()
                if point is None:
                    return
                self._write(point)
        finally:
            os.close(self._fd)

    def _write(self, point: RealtimeTracePoint) -> None:
        payload = {
            "record_kind": "realtime_trace_point",
            "telemetry_only": True,
            "production_fact": False,
            "name": point.name,
            "monotonic_ns": point.monotonic_ns,
            "attributes": dict(point.attributes),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode()
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(self._fd, view)
                view = view[written:]
        except OSError:
            LOGGER.warning("realtime trace JSONL export failed", exc_info=True)


def configure_realtime_trace_jsonl(path: Path | None) -> None:
    """Enable append-only JSONL export, or disable it with ``None``.

    The caller owns path selection.  The exporter never opens SQLite and each
    row is diagnostic-only.  Parent directories must already exist so an
    accidental environment value cannot create an unexpected directory tree.
    """
    global _EXPORTER, _EXPORT_PATH  # noqa: PLW0603 — singleton diagnostic sink.

    new_exporter: _JsonlExporter | None = None
    resolved: Path | None = None
    if path is not None:
        resolved = path.expanduser().resolve()
        if not resolved.parent.is_dir():
            msg = f"realtime trace parent does not exist: {resolved.parent}"
            raise FileNotFoundError(msg)
        new_exporter = _JsonlExporter(resolved)

    with _EXPORT_LOCK:
        old_exporter = _EXPORTER
        _EXPORTER = new_exporter
        _EXPORT_PATH = resolved
    if old_exporter is not None:
        old_exporter.close()


def realtime_trace_export_path() -> Path | None:
    """Return the configured diagnostic JSONL path, if export is enabled."""
    with _EXPORT_LOCK:
        return _EXPORT_PATH


@contextlib.contextmanager
def realtime_trace_context(**attributes: TraceValue) -> Iterator[None]:
    """Correlate nested provider/audio observations without changing APIs."""
    merged = dict(_TRACE_CONTEXT.get())
    merged.update(attributes)
    token = _TRACE_CONTEXT.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _TRACE_CONTEXT.reset(token)


def _export_point(point: RealtimeTracePoint) -> None:
    """Queue one diagnostic row; exporter failure never breaks production."""
    with _EXPORT_LOCK:
        exporter = _EXPORTER
        if exporter is not None:
            # Submit before a concurrent configure(None) can enqueue the old
            # writer's shutdown sentinel; this keeps the exported tail ordered.
            exporter.submit(point)


def record_realtime_trace(name: str, **attributes: TraceValue) -> RealtimeTracePoint:
    """Append one trace point to the bounded in-memory sink."""
    if not name:
        msg = "realtime trace name must be non-empty"
        raise ValueError(msg)
    merged_attributes = dict(_TRACE_CONTEXT.get())
    merged_attributes.update(attributes)
    frozen_attributes = MappingProxyType(merged_attributes)
    with _TRACE_LOCK:
        # Timestamp and append share the lock so concurrent producers cannot
        # expose points in reverse monotonic order to a snapshot consumer.
        point = RealtimeTracePoint(
            name=name,
            monotonic_ns=time.monotonic_ns(),
            attributes=frozen_attributes,
        )
        _TRACE_POINTS.append(point)
    _export_point(point)
    return point


def realtime_trace_snapshot() -> tuple[RealtimeTracePoint, ...]:
    """Return an immutable point-in-time copy in append order."""
    with _TRACE_LOCK:
        return tuple(_TRACE_POINTS)


def reset_realtime_trace() -> None:
    """Clear diagnostic state without touching canonical runtime state."""
    with _TRACE_LOCK:
        _TRACE_POINTS.clear()


def _close_exporter_at_exit() -> None:
    """Best-effort drain of the controlled JSONL queue on process exit."""
    with contextlib.suppress(Exception):
        configure_realtime_trace_jsonl(None)


atexit.register(_close_exporter_at_exit)


__all__ = [
    "RealtimeTracePoint",
    "TraceValue",
    "configure_realtime_trace_jsonl",
    "realtime_trace_context",
    "realtime_trace_export_path",
    "realtime_trace_snapshot",
    "record_realtime_trace",
    "reset_realtime_trace",
]
