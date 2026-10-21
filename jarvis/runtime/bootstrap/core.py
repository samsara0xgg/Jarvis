"""The boot's first stage: the environment, the Event Log and the memory store."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Final

from jarvis.deployment import RuntimePaths, load_env_file
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.turn_overlap import any_turn_in_flight, turn_activity_since


def load_runtime_env_and_trace(paths: RuntimePaths) -> None:
    """Load fill-only runtime env, then apply its optional trace destination."""
    load_env_file(paths.root)
    _configure_realtime_trace_export(paths)


def _configure_realtime_trace_export(paths: RuntimePaths) -> None:
    """Enable the controlled JSONL live-burn seam without touching Event Log."""
    trace_jsonl_raw = os.environ.get("JARVIS_REALTIME_TRACE_JSONL")
    if not trace_jsonl_raw:
        configure_realtime_trace_jsonl(None)
        return
    trace_jsonl = Path(trace_jsonl_raw).expanduser()
    if not trace_jsonl.is_absolute():
        trace_jsonl = paths.root / trace_jsonl
    if trace_jsonl.resolve() == paths.event_log.resolve():
        msg = "JARVIS_REALTIME_TRACE_JSONL must not target the production Event Log"
        raise RuntimeBootstrapError(msg)
    try:
        configure_realtime_trace_jsonl(trace_jsonl)
    except OSError as exc:
        msg = f"runtime: cannot open realtime trace JSONL {trace_jsonl}: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def _daemon_idle(event_log_path: Path) -> bool:
    """No turn in flight and no turn, answer or playback row in the last 15 s (ADR 0164).

    The same in-flight probe the voice session's quiet clock uses
    (``_turn_working`` in ``inherent_loop``); an unreadable log counts as busy.
    """
    now_ms = int(time.time() * 1000)
    try:
        with contextlib.closing(
            open_runtime_event_log(event_log_path, deadline=time.monotonic() + 0.25),
        ) as conn:
            return not (
                any_turn_in_flight(conn, since_ms=now_ms - _IDLE_WORKING_WINDOW_MS)
                or turn_activity_since(conn, since_ms=now_ms - _IDLE_QUIET_MS)
            )
    except sqlite3.Error:
        return False


_IDLE_WORKING_WINDOW_MS: Final[int] = 5 * 60 * 1000


_IDLE_QUIET_MS: Final[int] = 15 * 1000
