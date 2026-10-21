"""The boot's first stage: the environment, the process's settings, the Event Log, the memory store.

:func:`open_core` is the only stage that opens something the others cannot rebuild, so it hands
the boot's :class:`~contextlib.ExitStack` the call that closes it (ADR 0372).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.deployment import RuntimePaths, load_env_file
from jarvis.deployment.launchd import logs_dir
from jarvis.execution.path_resolver import FileTargetsConfigError, configure_file_targets
from jarvis.runtime.bootstrap.devices import _settings_drift
from jarvis.runtime.config import RuntimeBootstrapError
from jarvis.shared import lang, llm_io_log
from jarvis.shared.clock import SystemClock, ZoneError
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl
from jarvis.state import places as place_state
from jarvis.state.event_log import open_event_log, open_runtime_event_log
from jarvis.state.locate_wait import LocateWaits
from jarvis.state.memory_db import open_memory_db
from jarvis.state.turn_overlap import any_turn_in_flight, turn_activity_since
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import ExitStack

    from jarvis.runtime.config import RuntimeConfig
    from jarvis.runtime.settings_owner import SettingsDrift
    from jarvis.shared.device_link import DeviceLink
    from jarvis.state.memory_db import MemorySettings

_IDLE_WORKING_WINDOW_MS: Final[int] = 5 * 60 * 1000
_IDLE_QUIET_MS: Final[int] = 15 * 1000


@dataclass(frozen=True)
class Core:
    """What every later stage builds on: the clock, the Event Log, the memory store and the devices.

    ``conn`` is the process's one open Event Log connection; the boot's stack closes it if a later
    stage fails, and the runtime owns it once the boot succeeds. ``device`` is the paired terminal's
    call on a brain (ADR 0170), where TimeSink and git are the terminal's.
    """

    clock: SystemClock
    conn: sqlite3.Connection
    memory: MemorySettings
    locate_waits: LocateWaits
    phone_events: BrainEvents | None
    terminal_hub: TerminalHub | None
    device: DeviceLink | None
    settings_drift: SettingsDrift | None


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


def _install_open_path(block: Mapping[str, Any]) -> None:
    """Hand `tools.open_path` to L4's resolver; malformed bookmarks fail the boot."""
    try:
        configure_file_targets(block)
    except FileTargetsConfigError as exc:
        msg = f"runtime: tools.open_path invalid: {exc}"
        raise RuntimeBootstrapError(msg) from exc


def install_process_globals(config: RuntimeConfig, paths: RuntimePaths) -> None:
    """Set what this process keeps in module state: the language, the model-call log, the bookmarks.

    Nothing undoes them when the boot fails after this: the next boot sets them again.
    """
    lang.set_language(config.language)
    llm_io_log.configure(
        logs_dir(paths.root) / "llm-io.jsonl" if config.diagnostics.log_llm_io else None,
    )
    _install_open_path(config.tools.open_path)


def open_core(config: RuntimeConfig, paths: RuntimePaths, stack: ExitStack) -> Core:
    """Check the boot can start, then open the Event Log and the memory store.

    The prompt file and the zone database are checked first, so a boot that cannot succeed opens
    nothing. The Event Log connection is closed by ``stack`` if a later stage fails.
    """
    if not config.prompt_path.is_file():
        msg = f"runtime: prompt file {config.prompt_path} does not exist"
        raise RuntimeBootstrapError(msg)
    clock = SystemClock(config.zone)
    try:
        clock.check_rule()
    except ZoneError as exc:
        msg = f"runtime: {exc}"
        raise RuntimeBootstrapError(msg) from exc

    install_process_globals(config, paths)
    conn = open_event_log(paths.event_log)
    stack.callback(conn.close)
    memory = config.memory
    # ADR 0170: a brain's TimeSink and git are its terminal's, so what reads them asks the hub.
    locate_waits = LocateWaits()
    phone_events = (
        BrainEvents(conn, locate_waits)
        if config.role == "brain" or config.listen_addresses else None
    )
    terminal_hub = TerminalHub(events=phone_events) if config.role == "brain" else None
    settings_drift = _settings_drift(terminal_hub, paths, config.raw)
    # ADR 0068: refuse a memory.db a newer Jarvis wrote before anything writes to it.
    open_memory_db(memory.db_path).close()
    # ADR 0234: home and school move out of the settings into the places table, once.
    place_state.migrate_settings(memory.db_path, config.transit_places)
    return Core(
        clock=clock,
        conn=conn,
        memory=memory,
        locate_waits=locate_waits,
        phone_events=phone_events,
        terminal_hub=terminal_hub,
        device=None if terminal_hub is None else terminal_hub.call,
        settings_drift=settings_drift,
    )


def _daemon_idle(event_log_path: Path) -> bool:
    """No turn in flight and no turn, answer or playback row in the last 15 s (ADR 0164).

    The same in-flight probe the voice session's quiet clock uses
    (``_turn_working`` in ``runtime/daemon/listen_ports.py``); an unreadable log counts as busy.
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
