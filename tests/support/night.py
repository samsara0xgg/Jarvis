"""The night run's world: a fake Mac, a clock that moves wall and monotonic time together."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.deployment import bootstrap_runtime
from jarvis.deployment.night_power import Battery, Presence
from jarvis.runtime.night_run import NightRun, NightSettings
from jarvis.state.event_log import open_event_log

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")


def night_ms(hour: int, minute: int = 0, *, day: int = 29) -> int:
    """Local time on September ``day``; day 31 is October 1."""
    at = datetime(2026, 9, 1, hour, minute, tzinfo=ZONE) + timedelta(days=day - 1)
    return int(at.timestamp() * 1000)


class FakeMacPower:
    """Answers like MacPower and writes down every change asked of it."""

    def __init__(self) -> None:
        self.level: float | None = 0.8
        # Every device that takes a mute, by UID, and the ones behind the default output.
        self.mutes: dict[str, bool] = {"speakers": False, "phones": False, "tv": False}
        self.default: tuple[str, ...] | None = ("speakers", "phones")
        self.seen: Presence | None = Presence(locked=True, idle_s=9_999.0)
        self.cell: Battery | None = Battery(on_battery=False, percent=90)
        self.held: dict[int, float] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._ids = 40

    def hold_awake(self, seconds: float) -> int | None:
        self._ids += 1
        self.held[self._ids] = seconds
        self.calls.append(("hold", round(seconds)))
        return self._ids

    def release(self, assertion: int) -> bool:
        self.calls.append(("release", assertion))
        return self.held.pop(assertion, None) is not None

    def sleep_display(self) -> bool:
        self.calls.append(("display_sleep",))
        return True

    def brightness(self) -> float | None:
        return self.level

    def set_brightness(self, level: float) -> bool:
        self.level = level
        return True

    def outputs(self) -> dict[str, bool | None] | None:
        if self.default is None:
            return None
        return {uid: self.mutes.get(uid) for uid in self.default}

    def is_muted(self, uid: str) -> bool | None:
        return self.mutes.get(uid)

    def set_muted(self, uid: str, *, muted: bool) -> bool:
        if uid not in self.mutes:
            return False
        self.mutes[uid] = muted
        self.calls.append(("mute" if muted else "unmute", uid))
        return True

    def presence(self) -> Presence | None:
        return self.seen

    def battery(self) -> Battery | None:
        return self.cell


class _Clock:
    """Wall time and the monotonic clock, moved together."""

    def __init__(self, ms: int) -> None:
        self.ms = ms
        self.mono = 5_000.0

    def go(self, *, to: int | None = None, seconds: float = 0.0) -> None:
        step_ms = to - self.ms if to is not None else int(seconds * 1000)
        self.ms += step_ms
        self.mono += step_ms / 1000


class NightFixture:
    def __init__(self, tmp_path: Path, *, at: int) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.mac = FakeMacPower()
        self.clock = _Clock(at)
        self.night = self.boot(self.mac)

    def controller(self, mac: FakeMacPower) -> NightRun:
        return NightRun(
            self.paths.event_log,
            NightSettings(),
            mac,
            zone=ZONE,
            now_ms=lambda: self.clock.ms,
            monotonic=lambda: self.clock.mono,
            sleep=lambda _s: None,
        )

    def boot(self, mac: FakeMacPower) -> NightRun:
        """A daemon start: a fresh controller over the same log, read back."""
        night = self.controller(mac)
        night.recover()
        return night

    def rows(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (etype, json.loads(payload))
            for etype, payload in self.conn.execute(
                "SELECT type, payload_json FROM events WHERE type LIKE 'night.%' ORDER BY id",
            )
        ]

    def close(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self.conn.close()
