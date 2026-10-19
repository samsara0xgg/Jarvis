"""The Mac terminal copies TimeSink and Claude Code sessions to the brain on a schedule (ADR 0294).

``mac_ship`` in the terminal's config turns it on (off by default). One asyncio task wakes every
``poll_interval_s``, runs one pass on a worker thread, and goes back to sleep; the pass reads
TimeSink's database read-only (:mod:`mac_ship_timesink`) and the Claude Code logs of the
configured accounts (:mod:`mac_ship_sessions`), ships the Markdown day reports of his Claude Code
work (:mod:`mac_ship_days`, ADR 0303), ships the app use of his iPhone and iPad that the Mac's
Screen Time sync holds (:mod:`mac_ship_devices`, ADR 0304) and what else his devices sync to it
(:mod:`mac_ship_records`, ADR 0305), and sends what changed to the brain's
``/inherent/imports`` routes with this device's token. Its cursors live in
``<runtime root>/mac-ship/state.db`` and move only after the brain has kept what they stand for.

Layer rules: L5 surface; reads only files its own config names.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from jarvis.surface import (
    mac_ship_days,
    mac_ship_devices,
    mac_ship_records,
    mac_ship_sessions,
    mac_ship_timesink,
)
from jarvis.surface.mac_ship_core import Conflict, Report, ShipFailed, Sink, State
from jarvis.surface.mac_ship_timesink import TimesinkConfig

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)

STATE_PATH: Final = Path("mac-ship") / "state.db"
"""Under the terminal's runtime root."""
IMPORTS_PATH: Final = "/inherent/imports/"
HTTP_TIMEOUT_S: Final = 120.0
_DEFAULT_POLL_S: Final = 300.0
_DEFAULT_TIMESINK: Final = "~/Library/Application Support/TimeSink/timesink.sqlite"
_DEFAULT_ROOTS: Final = (("a", "~/.claude/projects"), ("b", "~/.claude-b/projects"))
_DEFAULT_DESKTOP: Final = "~/Library/Application Support/Claude/claude-code-sessions"


@dataclass(frozen=True)
class ShipConfig:
    """What this machine ships; ``None`` for a part means that part is off."""

    state_path: Path
    poll_interval_s: float = _DEFAULT_POLL_S
    timesink: TimesinkConfig | None = None
    session_roots: tuple[tuple[str, Path], ...] = ()
    desktop_titles: Path | None = None
    days_dir: Path | None = None
    device_names: Path | None = None
    """Where the iPhone/iPad app step keeps its names (ADR 0304); ``None``: that step is off."""
    device_records: Path | None = None
    """The same file for the iPhone/iPad records step (ADR 0305); ``None``: that step is off."""


def _section(parent: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = parent.get(key)
    return value if isinstance(value, dict) else None


def _number(section: Mapping[str, Any], key: str, default: float) -> float:
    value = section.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def ship_config(config: Mapping[str, Any], runtime_root: Path) -> ShipConfig | None:
    """The shipping this machine's config asks for, or ``None`` when ``mac_ship.enabled`` is off."""
    section = _section(config, "mac_ship")
    if section is None or section.get("enabled") is not True:
        return None
    timesink_part = _section(section, "timesink") or {}
    timesink = None
    if timesink_part.get("enabled", True) is not False:
        pictures = _section(timesink_part, "images") or {}
        path = timesink_part.get("db_path")
        timesink = TimesinkConfig(
            Path(path if isinstance(path, str) else _DEFAULT_TIMESINK).expanduser(),
            images=pictures.get("enabled", True) is not False,
            image_scale=_number(pictures, "scale", 1.0),
            image_quality=_number(pictures, "quality", 1.0),
            images_per_poll=int(_number(pictures, "per_poll", 1000)),
        )
    code = _section(section, "claude_code") or {}
    roots: tuple[tuple[str, Path], ...] = ()
    desktop = None
    if code.get("enabled", True) is not False:
        listed = code.get("roots")
        pairs = (
            [(str(r.get("account")), str(r.get("dir"))) for r in listed if isinstance(r, dict)]
            if isinstance(listed, list) else list(_DEFAULT_ROOTS)
        )
        roots = tuple((account, Path(where).expanduser()) for account, where in pairs)
        titles = code.get("desktop_titles")
        desktop = Path(titles if isinstance(titles, str) else _DEFAULT_DESKTOP).expanduser()
    days = _section(section, "claude_code_days") or {}
    devices = _section(section, "device_apps") or {}
    records = _section(section, "device_records") or {}
    names = runtime_root / STATE_PATH.parent / mac_ship_devices.NAMES_FILE
    return ShipConfig(
        runtime_root / STATE_PATH, max(1.0, _number(section, "poll_interval_s", _DEFAULT_POLL_S)),
        timesink, roots, desktop,
        None if days.get("enabled", True) is False else runtime_root / mac_ship_days.DIRNAME,
        None if devices.get("enabled", True) is False else names,
        None if records.get("enabled", True) is False else names,
    )


class HttpSink:
    """The brain's ``/inherent/imports`` routes, with this device's token."""

    def __init__(
        self, brain_url: str, token: str, transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Nothing is sent until a pass runs."""
        self._url = brain_url.rstrip("/") + IMPORTS_PATH
        self._http = httpx.Client(
            timeout=HTTP_TIMEOUT_S, transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def _answer(self, send: Any, what: str) -> dict[str, Any]:  # noqa: ANN401 - an httpx call
        try:
            answer = send()
        except httpx.HTTPError as exc:
            msg = f"the brain is not reachable ({type(exc).__name__})"
            raise ShipFailed(msg) from None
        if answer.status_code == httpx.codes.CONFLICT:
            msg = f"the brain holds other bytes for {what}"
            raise Conflict(msg)
        if answer.status_code not in (200, 201):
            msg = f"the brain answered {answer.status_code} for {what}"
            raise ShipFailed(msg)
        body = answer.json() if answer.content else {}
        return body if isinstance(body, dict) else {}

    def send_entries(self, source: str, body: bytes) -> None:
        """POST one JSONL body."""
        self._answer(lambda: self._http.post(self._url + source, content=body), source)

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        """PUT one file; the SHA-256 the brain kept."""
        got = self._answer(
            lambda: self._http.put(f"{self._url}{source}/files/{relpath}", content=data), relpath,
        )
        return str(got.get("sha256", ""))


class Shipper:
    """One machine's shipping: a pass at a time over what its config names."""

    def __init__(self, config: ShipConfig, sink: Sink, state: State | None = None) -> None:
        """``state`` defaults to the file the config names."""
        self.config = config
        self._sink = sink
        self.state = state or State(config.state_path)
        self._desktop: dict[Path, tuple[int, str, str, str]] = {}

    def poll(self, now_ms: int | None = None) -> Report:
        """One pass; a part the brain refuses is retried next pass, the other part still runs."""
        now = int(time.time() * 1000) if now_ms is None else now_ms
        report = Report()
        steps: list[tuple[str, Callable[[], None]]] = []
        timesink = self.config.timesink
        if timesink is not None:
            steps.append(("timesink", lambda: mac_ship_timesink.mirror(
                timesink, self.state, self._sink, report, now,
            )))
        if self.config.session_roots:
            steps.append(("claude-code", lambda: mac_ship_sessions.scan(
                self.config.session_roots,
                mac_ship_sessions.desktop_titles(self.config.desktop_titles, self._desktop),
                self.state, self._sink, report, now,
            )))
        days_dir = self.config.days_dir
        if days_dir is not None:
            steps.append(("claude-code-days", lambda: mac_ship_days.ship(
                days_dir, self.state, self._sink, report, now,
            )))
        names_path = self.config.device_names
        if names_path is not None:
            steps.append(("device-apps", lambda: mac_ship_devices.ship(
                names_path, self.state, self._sink, report, now,
            )))
        records_names = self.config.device_records
        if records_names is not None:
            steps.append(("device-records", lambda: mac_ship_records.ship(
                records_names, self.state, self._sink, report, now,
            )))
        for name, step in steps:
            try:
                step()
            except ShipFailed as exc:
                LOGGER.warning("mac-ship: %s stopped: %s; it resumes next pass", name, exc)
                report.failed.append(f"{name}: {exc}")
            except OSError as exc:
                LOGGER.warning("mac-ship: %s could not read (%s)", name, type(exc).__name__)
                report.failed.append(f"{name}: {type(exc).__name__}")
        return report


async def run_shipper(config: ShipConfig, brain_url: str, token: str) -> None:
    """Pass after pass until cancelled. A pass that raises anything else is logged, not fatal."""
    sink = HttpSink(brain_url, token)
    shipper = Shipper(config, sink)
    LOGGER.info("mac-ship: copying TimeSink, Claude Code, day reports, device apps, device records")
    try:
        while True:
            try:
                report = await asyncio.to_thread(shipper.poll)
                LOGGER.info(
                    "mac-ship: pass done: %d entries (%d bytes), %d files (%d bytes)%s",
                    sum(report.entries.values()), report.entry_bytes, report.files,
                    report.file_bytes, f", failed: {report.failed}" if report.failed else "",
                )
            except Exception:
                LOGGER.exception("mac-ship: a pass failed; trying again next time")
            await asyncio.sleep(config.poll_interval_s)
    finally:
        sink.close()
        shipper.state.close()
