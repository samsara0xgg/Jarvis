"""ADR 0093: the night run — the owner sleeps, the Mac keeps running, quiet and dark.

One controller owns a run. Starting takes the keep-awake assertion with the
deadline as its OS timeout. A few seconds later the run writes down the
brightness and sound it found, then dims the built-in panel, mutes the output
and puts the display to sleep. At the deadline only the assertion goes. Input
on an unlocked session is the owner coming back: before their morning it is a look,
and the display sleeps again after a quiet minute; at or after it, or when they
say so, brightness and sound come back and the run ends. Every step is a
``night.*`` event, and a restarted daemon picks the run up from them.

Every step runs under one lock and off the event loop: in the tool's worker
thread, in a route's thread, or in the two-second tick.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final, Protocol

from jarvis.deployment.night_power import Volume
from jarvis.execution.night_tools import MAX_HOURS, MIN_HOURS
from jarvis.shared import lang
from jarvis.state.event_log import emit_event, iter_events_of_types, open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.deployment.night_power import Battery, Presence

LOGGER = logging.getLogger(__name__)

TYPES: Final = ("night.started", "night.darkened", "night.released", "night.ended")
# The bedtime card's few seconds: the goodnight line is said, then the screen goes.
DARK_AFTER_S: Final = 8.0
# A look before the morning ends after this long without input.
LOOK_IDLE_S: Final = 60.0
# The built-in panel's level for the night, so a look does not glare.
DIM: Final = 0.12
# On battery at or below this, the Mac is let go early, so it sleeps with charge left.
BATTERY_FLOOR: Final = 10
TICK_S: Final = 2.0
_GRACE_S: Final = 5.0  # input this soon after the screen went dark is the same visit
_BUSY_WAIT_MS: Final = 20_000  # a line still playing or a wake capture delays the dark this long
_OURS: Final = 0.05  # a panel this close to the night level is still the run's
_LOOK_WINDOW_MS: Final = 10 * 3_600_000  # a start further than this from the morning gets no looks
_BATTERY_EVERY_S: Final = 60.0
_LOOKBACK_MS: Final = 3 * 24 * 3_600_000
_FADE_STEPS: Final = 10
_FADE_STEP_S: Final = 0.06
# What End says, by what it put back.
_ENDED: Final = {
    (): "night.ended",
    ("brightness",): "night.ended_brightness",
    ("volume",): "night.ended_volume",
    ("brightness", "volume"): "night.ended_restored",
}


class NightPower(Protocol):
    """The Mac switches (``jarvis.deployment.night_power.MacPower``)."""

    def hold_awake(self, seconds: float) -> int | None:
        """Take the keep-awake assertion; its id, or None."""

    def release(self, assertion: int) -> bool:
        """Let the assertion go."""

    def sleep_display(self) -> bool:
        """Put the displays to sleep now."""

    def brightness(self) -> float | None:
        """The built-in panel's level, 0-1."""

    def set_brightness(self, level: float) -> bool:
        """Set the built-in panel's level."""

    def volume(self) -> Volume | None:
        """The output level and mute."""

    def set_volume(self, volume: Volume) -> bool:
        """Set the output level and mute."""

    def presence(self) -> Presence | None:
        """Lock state and input idle time."""

    def battery(self) -> Battery | None:
        """Charge and power source; None without a battery."""


@dataclass(frozen=True)
class NightSettings:
    """``night:`` in jarvis.yaml: the default length and the owner's morning."""

    hours: float = 2.0
    morning: clock = clock(6, 0)


def night_settings(config: Mapping[str, Any]) -> NightSettings:
    """Read ``night: {hours, morning}``; a value that does not parse keeps its default."""
    block = config.get("night")
    if not isinstance(block, Mapping):
        return NightSettings()
    default = NightSettings()
    hours = block.get("hours", default.hours)
    number = isinstance(hours, int | float) and not isinstance(hours, bool)
    if not number or not MIN_HOURS <= hours <= MAX_HOURS:
        LOGGER.warning("night.hours %r is not %g-%g hours; using %g", hours, MIN_HOURS,
                       MAX_HOURS, default.hours)
        hours = default.hours
    morning = default.morning
    raw = block.get("morning")
    if raw is not None:
        try:
            parsed = clock.fromisoformat(str(raw))
        except ValueError:
            LOGGER.warning("night.morning %r is not HH:MM; using 06:00", raw)
        else:
            morning = parsed
    return NightSettings(hours=float(hours), morning=morning)


@dataclass
class _Night:
    """The run in progress; the events are its record, this is the working copy."""

    night_id: str
    started_ms: int
    until_ms: int
    wake_at_ms: int | None
    assertion: int | None = None
    saved: dict[str, Any] | None = None
    darkened_ms: int | None = None
    released_ms: int | None = None
    dark_at: float | None = None  # monotonic: when the display last went to sleep
    look_since: float | None = None  # monotonic: the owner is having a look


def _never_busy() -> bool:
    return False


class NightRun:
    """The one night run. The daemon sets ``busy``: a line plays or a wake capture holds sound."""

    def __init__(  # noqa: PLR0913 — the clocks and the zone are the test seams.
        self,
        event_log: Path,
        settings: NightSettings,
        power: NightPower,
        *,
        zone: tzinfo | None = None,
        now_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind the log and the Mac; nothing runs until :meth:`run`."""
        self._path = event_log
        self._settings = settings
        self._power = power
        self._zone = zone
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._mono = monotonic
        self._sleep = sleep
        self._lock = threading.RLock()
        self._night: _Night | None = None
        self._last: dict[str, Any] | None = None
        self._laptop = False
        self._battery_at = -math.inf
        self._serving = False
        self.busy: Callable[[], bool] = _never_busy

    # --- entry points: any thread ------------------------------------------

    def start(
        self,
        *,
        hours: float | None,
        until: clock | None,
        source: str,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a run; the one already running answers instead of a second."""
        with self._lock:
            if not self._serving:
                return {"status": "unavailable", "spoken": lang.t("night.unavailable")}
            night = self._night
            if night is not None:
                return {
                    "status": "already",
                    "until": self._hhmm(night.until_ms),
                    "spoken": lang.t("night.already", until=self._said(night.until_ms)),
                }
            now = self._now_ms()
            until_ms = self._until(now, hours, until)
            self._laptop = self._power.battery() is not None
            assertion = self._power.hold_awake((until_ms - now) / 1000)
            night = _Night(
                night_id=uuid.uuid4().hex,
                started_ms=now,
                until_ms=until_ms,
                wake_at_ms=self._wake_at(now),
                assertion=assertion,
            )
            try:
                with self._log() as conn:
                    emit_event(
                        conn,
                        type="night.started",
                        payload={
                            "night_id": night.night_id,
                            "until_ms": until_ms,
                            "wake_at_ms": night.wake_at_ms,
                            "source": source,
                            "guarded": assertion is not None,
                        },
                        correlation=None if action_id is None else {"action_id": action_id},
                        ts_epoch_ms=now,
                    )
            except BaseException:
                if assertion is not None:
                    self._power.release(assertion)
                raise
            self._night = night
            key = "night.started" if assertion is not None else "night.started_unguarded"
            return {
                "status": "started",
                "until": self._hhmm(until_ms),
                "spoken": lang.t(key, until=self._said(until_ms), seconds=int(DARK_AFTER_S)),
            }

    def darken_now(self) -> None:
        """Skip the bedtime card's wait: dim, mute and sleep the display now."""
        with self._lock:
            night = self._night
            if night is not None and night.saved is None:
                self._darken(night)

    def end(self, *, action_id: str | None = None) -> dict[str, Any]:
        """The owner is up, or changed their mind before the screen went."""
        with self._lock:
            night = self._night
            if night is None:
                return {"status": "none", "spoken": lang.t("night.none")}
            if night.saved is None:
                self._finish(night, "cancelled", action_id=action_id)
                return {"status": "cancelled", "spoken": lang.t("night.cancelled")}
            restored = self._finish(night, "ended", action_id=action_id)
            back = tuple(name for name in ("brightness", "volume") if restored[name])
            return {"status": "ended", "spoken": lang.t(_ENDED[back])}

    def snapshot(self) -> dict[str, Any]:
        """What the companion draws: the run now, and the last one that ended."""
        with self._lock:
            night = self._night
            shown = None
            if night is not None:
                phase = (
                    "starting" if night.saved is None
                    else "glance" if night.look_since is not None
                    else "dark"
                )
                shown = {
                    "id": night.night_id,
                    "phase": phase,
                    "started_ms": night.started_ms,
                    "until_ms": night.until_ms,
                    "wake_at_ms": night.wake_at_ms,
                    "dark_at_ms": night.darkened_ms
                    or night.started_ms + int(DARK_AFTER_S * 1000),
                    "released_ms": night.released_ms,
                    "guarded": night.assertion is not None,
                }
            return {
                "night": shown,
                "last": self._last,
                "hours": self._settings.hours,
                "laptop": self._laptop,
            }

    # --- the daemon's loop ---------------------------------------------------

    async def run(self) -> None:
        """Pick up a run the log says is open, then tick until the daemon stops."""
        try:
            await asyncio.to_thread(self.recover)
        except Exception:
            LOGGER.exception("night run: could not read the last run back")
            self._serving = True
        try:
            while True:
                await asyncio.sleep(TICK_S)
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:
                    LOGGER.exception("night run: tick failed")
        finally:
            self._serving = False

    def tick(self) -> None:
        """Take whichever step is due: dark, the deadline, a look, the return."""
        with self._lock:
            night = self._night
            if night is None:
                return
            now = self._now_ms()
            if night.saved is None:
                due = night.started_ms + int(DARK_AFTER_S * 1000)
                if now >= due and (not self.busy() or now >= due + _BUSY_WAIT_MS):
                    self._darken(night)
                return
            if night.released_ms is None:
                if now >= night.until_ms:
                    self._release(night, "deadline")
                elif self._battery_low():
                    self._release(night, "battery")
            self._watch(night, now)

    def recover(self) -> None:
        """The daemon's boot: rebuild the open run and the last ended one, then serve."""
        with self._lock:
            self._serving = True
            rows = self._fold()
            ended = [row for row in rows.values() if "ended_ms" in row]
            if ended:
                last = ended[-1]
                self._last = self._summary(last["night"], last["ended_ms"], last["reason"],
                                           last.get("restored") or {})
            if not rows:
                return
            latest = list(rows.values())[-1]
            if "ended_ms" in latest:
                return
            night: _Night = latest["night"]
            night.dark_at = self._mono()
            now = self._now_ms()
            if night.released_ms is None:
                if now < night.until_ms:
                    night.assertion = self._power.hold_awake((night.until_ms - now) / 1000)
                else:
                    self._release(night, "deadline")
            self._laptop = self._power.battery() is not None
            self._night = night

    # --- steps: called with the lock held ---------------------------------------

    def _darken(self, night: _Night) -> None:
        sound = self._power.volume()
        level = self._power.brightness()
        saved = {
            "brightness": level,
            "volume": None if sound is None else sound.level,
            "muted": None if sound is None else sound.muted,
        }
        now = self._now_ms()
        with self._log() as conn:
            emit_event(
                conn,
                type="night.darkened",
                payload={"night_id": night.night_id, "saved": saved},
                ts_epoch_ms=now,
            )
        night.saved = saved
        night.darkened_ms = now
        if level is not None and level > DIM:
            self._power.set_brightness(DIM)
        if sound is not None and not sound.muted:
            self._power.set_volume(Volume(level=sound.level, muted=True))
        self._power.sleep_display()
        night.dark_at = self._mono()

    def _release(self, night: _Night, reason: str) -> None:
        if night.assertion is not None:
            self._power.release(night.assertion)
            night.assertion = None
        night.released_ms = self._now_ms()
        with self._log() as conn:
            emit_event(
                conn,
                type="night.released",
                payload={"night_id": night.night_id, "reason": reason},
                ts_epoch_ms=night.released_ms,
            )

    def _watch(self, night: _Night, now: int) -> None:
        seen = self._power.presence()
        if seen is None:
            return
        mono = self._mono()
        dark_for = mono - (night.dark_at if night.dark_at is not None else mono)
        back = not seen.locked and seen.idle_s < dark_for - _GRACE_S
        morning = night.wake_at_ms is None or now >= night.wake_at_ms
        if back and morning:
            self._finish(night, "returned")
        elif night.look_since is None:
            if back:
                night.look_since = mono
        elif seen.locked or seen.idle_s >= LOOK_IDLE_S:
            self._power.sleep_display()
            night.dark_at = self._mono()
            night.look_since = None

    def _finish(
        self, night: _Night, reason: str, *, action_id: str | None = None,
    ) -> dict[str, bool]:
        restored = self._restore(night.saved)
        if night.assertion is not None:
            self._power.release(night.assertion)
            night.assertion = None
        ended_ms = self._now_ms()
        with self._log() as conn:
            emit_event(
                conn,
                type="night.ended",
                payload={"night_id": night.night_id, "reason": reason, "restored": restored},
                correlation=None if action_id is None else {"action_id": action_id},
                ts_epoch_ms=ended_ms,
            )
        self._night = None
        self._last = self._summary(night, ended_ms, reason, restored)
        return restored

    def _restore(self, saved: Mapping[str, Any] | None) -> dict[str, bool]:
        """Put back what the run changed and nobody changed since."""
        restored = {"brightness": False, "volume": False}
        if saved is None:
            return restored
        before = saved.get("brightness")
        if isinstance(before, int | float) and before > DIM:
            now = self._power.brightness()
            if now is not None and abs(now - DIM) <= _OURS:
                for step in range(1, _FADE_STEPS):
                    self._power.set_brightness(now + (before - now) * step / _FADE_STEPS)
                    self._sleep(_FADE_STEP_S)
                restored["brightness"] = self._power.set_brightness(float(before))
        level = saved.get("volume")
        if isinstance(level, int) and saved.get("muted") is False:
            sound = self._power.volume()
            if sound is not None and sound.muted:
                restored["volume"] = self._power.set_volume(Volume(level=level, muted=False))
        return restored

    def _battery_low(self) -> bool:
        mono = self._mono()
        if mono - self._battery_at < _BATTERY_EVERY_S:
            return False
        self._battery_at = mono
        cell = self._power.battery()
        return cell is not None and cell.on_battery and cell.percent <= BATTERY_FLOOR

    # --- reading the log -------------------------------------------------------------

    def _log(self) -> closing[sqlite3.Connection]:
        return closing(open_runtime_event_log(self._path))

    def _fold(self) -> dict[str, dict[str, Any]]:
        """Every run of the last few days, in start order, from its events."""
        rows: dict[str, dict[str, Any]] = {}
        since = self._now_ms() - _LOOKBACK_MS
        with self._log() as conn:
            for event in iter_events_of_types(conn, TYPES, since_epoch_ms=since):
                payload = event.payload
                night_id = payload.get("night_id")
                if event.type == "night.started" and isinstance(night_id, str):
                    wake_at = payload.get("wake_at_ms")
                    rows[night_id] = {"night": _Night(
                        night_id=night_id,
                        started_ms=event.ts_epoch_ms,
                        until_ms=int(payload["until_ms"]),
                        wake_at_ms=wake_at if isinstance(wake_at, int) else None,
                    )}
                    continue
                row = rows.get(night_id) if isinstance(night_id, str) else None
                if row is None:
                    continue
                night: _Night = row["night"]
                if event.type == "night.darkened":
                    saved = payload.get("saved")
                    night.saved = dict(saved) if isinstance(saved, Mapping) else {}
                    night.darkened_ms = event.ts_epoch_ms
                elif event.type == "night.released":
                    night.released_ms = event.ts_epoch_ms
                else:
                    row["ended_ms"] = event.ts_epoch_ms
                    row["reason"] = str(payload.get("reason", ""))
                    restored = payload.get("restored")
                    row["restored"] = dict(restored) if isinstance(restored, Mapping) else {}
        return rows

    def _summary(
        self, night: _Night, ended_ms: int, reason: str, restored: Mapping[str, Any],
    ) -> dict[str, Any]:
        """The last run as the morning card tells it, with when the Mac first slept while held."""
        guard_end = night.released_ms or ended_ms
        slept_ms = None
        with self._log() as conn:
            sleeps = iter_events_of_types(conn, ("mac.sleeping",), since_epoch_ms=night.started_ms)
            for event in sleeps:
                if event.ts_epoch_ms < guard_end:
                    slept_ms = event.ts_epoch_ms
                break
        return {
            "id": night.night_id,
            "started_ms": night.started_ms,
            "until_ms": night.until_ms,
            "released_ms": night.released_ms,
            "ended_ms": ended_ms,
            "reason": reason,
            "slept_ms": slept_ms,
            "restored": {key: bool(restored.get(key)) for key in ("brightness", "volume")},
        }

    # --- clock ------------------------------------------------------------------------

    def _local(self, ms: int) -> datetime:
        # Naive local time when no zone is configured: its .timestamp() follows the
        # system's own daylight-saving rules, as the zone-aware one does.
        return datetime.fromtimestamp(ms / 1000, tz=self._zone)

    def _until(self, now_ms: int, hours: float | None, until: clock | None) -> int:
        longest = now_ms + int(MAX_HOURS * 3_600_000)
        if until is None:
            span = self._settings.hours if hours is None else hours
            return min(longest, now_ms + int(span * 3_600_000))
        return min(longest, self._next(now_ms, until))

    def _wake_at(self, now_ms: int) -> int | None:
        at = self._next(now_ms, self._settings.morning)
        return at if at - now_ms <= _LOOK_WINDOW_MS else None

    def _next(self, now_ms: int, when: clock) -> int:
        """The next time the local clock reads ``when``, after ``now_ms``."""
        local = self._local(now_ms)
        at = local.replace(hour=when.hour, minute=when.minute, second=0, microsecond=0)
        if at <= local:
            at += timedelta(days=1)
        return int(at.timestamp() * 1000)

    def _hhmm(self, ms: int) -> str:
        return self._local(ms).strftime("%H:%M")

    def _said(self, ms: int) -> str:
        return lang.spoken_time(self._local(ms))


__all__ = ["DARK_AFTER_S", "NightPower", "NightRun", "NightSettings", "night_settings"]
