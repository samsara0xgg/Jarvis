"""What the prompt and the Dashboard read of his day: ledger, doing lane, places, reports."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final

from jarvis.runtime.about_item import AboutItems
from jarvis.runtime.daily_report import (
    PLAN_SERVER,
    DailyReportService,
    DailySchedule,
    microsoft_plan,
)
from jarvis.runtime.day_rows import DayRows
from jarvis.runtime.doing_run import DoingSchedule
from jarvis.runtime.highlight_marks import HighlightMarks
from jarvis.runtime.ledger import LedgerContext
from jarvis.shared.pricing import load_pricing_table
from jarvis.state import device_reads, imports
from jarvis.state import places as place_state
from jarvis.state.attachments import ATTACHMENTS_DIRNAME
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.ledger import TURN_WAIT_S, LedgerSources, TerminalScreen
from jarvis.state.shares import shares_line

if TYPE_CHECKING:
    from datetime import datetime, tzinfo

    from jarvis.deployment import RuntimePaths
    from jarvis.runtime.config import DailyReportSettings, RuntimeConfig
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.shared.device_link import DeviceLink
    from jarvis.state.memory_db import (
        MemorySettings,
    )
    from jarvis.surface.terminal_link import TerminalHub


LOGGER = logging.getLogger(__name__)


def _ledger(
    config: RuntimeConfig, memory: MemorySettings | None, event_log: Path,
    device: DeviceLink | None = None, attachments: Path | None = None,
) -> LedgerContext | None:
    """ADR 0201: the one ledger both prompt renders ask; None without a ``ledger:`` block.

    On a brain (``device``, ADR 0170) its TimeSink is the terminal's. ``attachments`` is where his
    pictures are kept, which the day line of ADR 0235 reads.
    """
    settings = config.ledger
    if settings is None or memory is None:
        return None
    sources = LedgerSources(
        memory.db_path, event_log, config.observer.timesink_db_path, config.zone,
        None if device is None else TerminalScreen(device), attachments,
    )
    return LedgerContext(sources, settings)


def _highlight_marks(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths,
    device: DeviceLink | None,
) -> HighlightMarks:
    """ADR 0235: what `mark_highlight` does, over the same day line `GET /inherent/day` folds."""
    return HighlightMarks(
        paths.event_log,
        DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
    )


def _doing_schedule(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths, device: DeviceLink | None,
) -> DoingSchedule | None:
    """ADR 0302, 0310: the writer of the doing lane, over the day line `GET /inherent/day` folds."""
    settings = config.doing
    if settings is None:
        return None
    llm_config = config.raw.get("llm")
    return DoingSchedule(
        settings=settings,
        sources=DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            None if device is None else TerminalScreen(device, wait_s=TURN_WAIT_S),
            paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
        memory_db=memory.db_path,
        event_log_path=paths.event_log,
        llm_config=llm_config if isinstance(llm_config, Mapping) else {},
        pricing_table=load_pricing_table(config.pricing_path),
    )


def _about_items(
    config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths,
) -> AboutItems:
    """ADR 0298: the day line `GET /inherent/day` folds, from local stores only."""
    return AboutItems(
        paths.event_log,
        DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            attachments=paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
    )


def _day_rows(config: RuntimeConfig, memory: MemorySettings, paths: RuntimePaths) -> DayRows:
    """ADR 0391: the rows `my_day` names by id, from the local stores the day page folds."""
    return DayRows(
        paths.event_log,
        DaySources(
            config.zone, config.observer.timesink_db_path, memory.db_path,
            attachments=paths.artifacts_root / ATTACHMENTS_DIRNAME,
            shipped=paths.root / imports.IMPORTS_DIRNAME,
        ),
    )


_DAILY_REPORT_TIMEOUT_S: Final[float] = 900.0
"""ADR 0028 — a whole day served whole: the draft call read 300k tokens in 175 s on v4-pro
(2026-09-12), and a retry after a timeout would resend it all."""


def _places_block(
    memory_db: Path, event_log: Path, zone: tzinfo, now: datetime,
) -> str:
    """ADR 0234: the ``[Places he keeps]`` block of the system prompt; never fails the turn."""
    try:
        with contextlib.closing(open_runtime_event_log(event_log)) as conn:
            return place_state.prompt_block(memory_db, conn, int(now.timestamp() * 1000), zone)
    except Exception:  # a prompt block must never fail a turn.
        LOGGER.exception("places: the prompt block failed, left out")
        return ""


def _shares_line(event_log_path: Path) -> str | None:
    """ADR 0211: the line naming what Allen last saved from other apps, folded from the log."""
    with contextlib.closing(open_runtime_event_log(event_log_path)) as conn:
        return shares_line(conn)


def _codex_sessions_path(settings: DailyReportSettings) -> Path | None:
    """Codex's own session directory, only when ``daily_report.codex_sessions`` opts in."""
    if not settings.codex_sessions:
        return None
    root = Path.home() / ".codex" / "sessions"
    return root if root.is_dir() else None


def _reads_connected(hub: TerminalHub) -> bool:
    """Whether a connected terminal declared the TimeSink and git reads (ADR 0170)."""
    return any(tools >= device_reads.DEVICE_READS for _, tools in hub.connected())


def _daily_schedule(
    service: DailyReportService, event_log: Path, config: RuntimeConfig,
    hub: TerminalHub | None = None,
) -> DailySchedule | None:
    """``daily_report.at`` — when the daemon writes yesterday's report (ADR 0101); unset: off."""
    if config.daily_report.at is None:
        return None
    return DailySchedule(
        service, event_log_path=event_log, at=config.daily_report.at, zone=config.zone,
        device_ready=None if hub is None else partial(_reads_connected, hub),
    )


def _wire_plan_reader(service: DailyReportService, connections: PluginConnections) -> None:
    """ADR 0036: the report reads calendar and To Do itself, through the live connection."""
    if any(PLAN_SERVER in package.servers for package in connections.packages.values()):
        service.plan_reader = lambda start, end: microsoft_plan(
            connections.client_for(PLAN_SERVER), start, end
        )
