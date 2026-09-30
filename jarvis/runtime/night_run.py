"""ADR 0093: the night run — the owner sleeps, the Mac keeps running, quiet and dark.

One controller owns a run. Starting takes the keep-awake assertion with the
deadline as its OS timeout. A few seconds later the run writes down the
brightness and sound it found, then dims the built-in panel, mutes the output
and puts the display to sleep. Every fifteen seconds while it holds, it looks
at the agent sessions the daemon can see (``night_watch``) and writes down
each change. The hold lasts at least until the deadline, and past it while a
session works, renewed fifteen minutes at a time; it goes once none has worked
for three minutes, twelve hours after the start in any case. Input on an
unlocked session is the owner coming back: before their morning it is a look,
and the display sleeps again after a quiet minute; at or after it, or when
they say so, brightness and sound come back and the run ends. Every step is a
``night.*`` event, and a restarted daemon picks the run up from them; the
morning card's numbers are read back from them too.

Every step runs under one lock and off the event loop: in the tool's worker
thread, in a route's thread, or in the two-second tick. A look at the
sessions runs outside the lock, before the step that uses it.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final, Protocol

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

TYPES: Final = (
    "night.started", "night.darkened", "night.watched", "night.released", "night.ended",
)
# The bedtime card's few seconds: the goodnight line is said, then the screen goes.
DARK_AFTER_S: Final = 8.0
# A look before the morning ends after this long without input.
LOOK_IDLE_S: Final = 60.0
# The built-in panel's level for the night, so a look does not glare.
DIM: Final = 0.12
# On battery at or below this, the Mac is let go early, so it sleeps with charge left.
BATTERY_FLOOR: Final = 10
TICK_S: Final = 2.0
# How often the sessions are looked at while the run holds the Mac.
WATCH_S: Final = 15.0
# Past the deadline, the hold goes once no session has worked for this long.
SETTLE_S: Final = 180.0
# The hold goes this long after the start whatever the sessions do.
CAP_S: Final = MAX_HOURS * 3600
# Past the deadline the assertion is renewed this far ahead, when less than _RENEW_BEFORE_S is left.
RENEW_S: Final = 15 * 60.0
_RENEW_BEFORE_S: Final = 5 * 60.0
_MISSES: Final = 3  # a list that fails this many looks in a row counts as not seen
_ORDER: Final = ("startrail", "claude", "codex")  # the same session on two lists: the first wins
_TOTAL_NIGHTS: Final = 7
_GRACE_S: Final = 5.0  # input this soon after the screen went dark is the same visit
_BUSY_WAIT_MS: Final = 20_000  # a line still playing or a wake capture delays the dark this long
_OURS: Final = 0.05  # a panel this close to the night level is still the run's
_LOOK_WINDOW_MS: Final = 10 * 3_600_000  # a start further than this from the morning gets no looks
_BATTERY_EVERY_S: Final = 60.0
_LOOKBACK_MS: Final = 14 * 24 * 3_600_000
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

    def outputs(self) -> dict[str, bool | None] | None:
        """The default output's devices by UID: muted or not, None for no mute."""

    def is_muted(self, uid: str) -> bool | None:
        """Whether that device is muted now; None when it is gone."""

    def set_muted(self, uid: str, *, muted: bool) -> bool:
        """Mute or unmute that device."""

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


Row = dict[str, Any]


@dataclass
class _Night:
    """The run in progress; the events are its record, this is the working copy."""

    night_id: str
    started_ms: int
    until_ms: int
    wake_at_ms: int | None
    assertion: int | None = None
    hold_until_ms: int | None = None  # when the assertion's OS timeout lets the Mac go
    saved: dict[str, Any] | None = None
    darkened_ms: int | None = None
    released_ms: int | None = None
    release_reason: str | None = None
    dark_at: float | None = None  # monotonic: when the display last went to sleep
    look_since: float | None = None  # monotonic: the owner is having a look
    stay_since: float | None = None  # monotonic: before dark, the owner went to answer a session
    # Every change the looks saw (``night.watched``), in order: (ms, {seen, lists, sessions}).
    looks: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    lists: dict[str, list[Row]] = field(default_factory=dict)  # each list's rows at its last answer
    misses: dict[str, int] = field(default_factory=dict)  # looks in a row a list did not answer
    live: dict[str, Row] = field(default_factory=dict)  # the last look's rows, fresher than the log


@dataclass(frozen=True)
class _Tally:
    """The looks up to a moment: seeing, working, and since when all is quiet."""

    seen: bool = False  # the last look read at least one list
    busy: int = 0  # sessions working at the last look
    quiet_ms: int | None = None  # the look after the last one with work; None: never, or still
    worked: bool = False


def _busy(look: Mapping[str, Any]) -> int:
    return sum(1 for row in look["sessions"] if row.get("busy"))


def _tally(looks: list[tuple[int, dict[str, Any]]], *, until_ms: int | None = None) -> _Tally:
    seen, busy, quiet, worked = False, 0, None, False
    for ts, look in looks:
        if until_ms is not None and ts > until_ms:
            break
        seen, busy = bool(look.get("seen")), _busy(look)
        if busy:
            worked, quiet = True, None
        elif worked and quiet is None:
            quiet = ts
    return _Tally(seen=seen, busy=busy, quiet_ms=quiet, worked=worked)


def _working(tally: _Tally, now_ms: int) -> bool:
    """A session works now, or stopped less than the settle time ago."""
    settle = int(SETTLE_S * 1000)
    return tally.busy > 0 or (tally.quiet_ms is not None and now_ms - tally.quiet_ms < settle)


def _key(look: Mapping[str, Any]) -> tuple[Any, ...]:
    """What makes a look worth writing down: seeing, the lists, each session's state."""
    rows = sorted((str(row["id"]), str(row["st"]), bool(row["busy"])) for row in look["sessions"])
    return bool(look["seen"]), tuple(sorted(look["lists"].items())), tuple(rows)


def _nothing() -> bool:
    return False


class NightRun:
    """The one night run.

    The daemon sets ``busy`` (a line plays or a wake capture holds sound) and
    ``watch`` (``night_watch.NightWatch``: every list of agent sessions it can
    read). Without ``watch`` the deadline alone decides.
    """

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
        self._looked_at = -math.inf
        self._serving = False
        self.busy: Callable[[], bool] = _nothing
        self.watch: Callable[[], Mapping[str, list[Row] | None]] | None = None

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
        found = self._read() if self._serving and self._night is None else None
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
                hold_until_ms=None if assertion is None else until_ms,
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
            if found is not None:
                self._saw(night, found, now)
                self._looked_at = self._mono()
            working = _tally(night.looks).busy
            key = (
                "night.started_unguarded" if assertion is None
                else "night.started_watching" if working
                else "night.started"
            )
            return {
                "status": "started",
                "until": self._hhmm(until_ms),
                "working": working,
                "spoken": lang.t(key, until=self._said(until_ms), seconds=int(DARK_AFTER_S),
                                 working=working),
            }

    def darken_now(self) -> None:
        """Skip the bedtime card's wait: dim, mute and sleep the display now."""
        with self._lock:
            night = self._night
            if night is not None and night.saved is None:
                self._darken(night)

    def stay(self) -> None:
        """Before dark, the owner goes to answer a session: dark waits for a quiet minute."""
        with self._lock:
            night = self._night
            if night is not None and night.saved is None:
                night.stay_since = self._mono()

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
                staying = night.saved is None and night.stay_since is not None
                watch = self._story(night)
                tally = _tally(night.looks)
                cap_ms = night.started_ms + int(CAP_S * 1000)
                quiet_until = 0 if tally.quiet_ms is None else tally.quiet_ms + int(SETTLE_S * 1000)
                watch["release_ms"] = (
                    night.released_ms if night.released_ms is not None
                    else None if tally.busy
                    else min(cap_ms, max(night.until_ms, quiet_until))
                )
                shown = {
                    "id": night.night_id,
                    "phase": phase,
                    "started_ms": night.started_ms,
                    "until_ms": night.until_ms,
                    "cap_ms": cap_ms,
                    "wake_at_ms": night.wake_at_ms,
                    "dark_at_ms": None if staying else night.darkened_ms
                    or night.started_ms + int(DARK_AFTER_S * 1000),
                    "stay": staying,
                    "released_ms": night.released_ms,
                    "guarded": night.assertion is not None,
                    "watch": watch,
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
        """Look at the sessions when due, then take the step that is: dark, the hold, the return."""
        found = self._look()
        with self._lock:
            night = self._night
            if night is None:
                return
            now = self._now_ms()
            if found is not None and found[0] == night.night_id:
                self._saw(night, found[1], now)
            if night.saved is None and self._dark_due(night, now):
                self._darken(night)
            if night.released_ms is None:
                self._hold(night, now)
            if night.saved is not None:
                self._watch(night, now)

    def recover(self) -> None:
        """The daemon's boot: rebuild the open run and the last ended one, then serve."""
        with self._lock:
            self._serving = True
            rows = self._fold()
            self._last = self._last_of(rows)
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
                    night.hold_until_ms = night.until_ms
                else:
                    # Past the deadline: what the last look before the restart saw decides.
                    reason = self._release_reason(night, now)
                    if reason is None:
                        self._renew(night, now)
                    else:
                        self._release(night, reason)
            self._laptop = self._power.battery() is not None
            self._night = night

    # --- looking at the sessions ---------------------------------------------------

    def _read(self) -> Mapping[str, list[Row] | None] | None:
        """Every list's rows; None without a watch. Blocking, so never under the lock."""
        watch = self.watch
        if watch is None:
            return None
        try:
            return watch()
        except Exception:
            LOGGER.exception("night run: could not look at the sessions")
            return {}

    def _look(self) -> tuple[str, Mapping[str, list[Row] | None]] | None:
        """A look when one is due while the run holds the Mac: (the run's id, what it found)."""
        night = self._night
        if night is None or night.released_ms is not None:
            return None
        mono = self._mono()
        if mono - self._looked_at < WATCH_S:
            return None
        self._looked_at = mono
        found = self._read()
        return None if found is None else (night.night_id, found)

    def _saw(self, night: _Night, found: Mapping[str, list[Row] | None], now: int) -> None:
        """Fold one look in; a change in what works, waits or is seen is written down."""
        for name in {*found, *night.lists, *night.misses}:
            rows = found.get(name)
            if rows is None:
                night.misses[name] = night.misses.get(name, 0) + 1
                if night.misses[name] >= _MISSES:
                    night.lists.pop(name, None)
            else:
                night.misses[name] = 0
                night.lists[name] = rows
        merged: dict[str, Row] = {}
        rank = {name: index for index, name in enumerate(_ORDER)}
        for name in sorted(night.lists, key=lambda name: rank.get(name, len(rank))):
            for row in night.lists[name]:
                merged.setdefault(str(row["id"]), row)
        worked = {row["id"] for _, look in night.looks for row in look["sessions"] if row["busy"]}
        shown = [
            row for row in merged.values()
            if row["busy"] or row["st"] == "wait" or row["id"] in worked
        ]
        night.live = {str(row["id"]): row for row in shown}
        look = {
            "seen": bool(night.lists),
            "lists": {name: name in night.lists for name in sorted({*found, *night.misses})},
            "sessions": shown,
        }
        if night.looks and _key(night.looks[-1][1]) == _key(look):
            return
        with self._log() as conn:
            emit_event(
                conn,
                type="night.watched",
                payload={"night_id": night.night_id, **look},
                ts_epoch_ms=now,
            )
        night.looks.append((now, look))

    def _story(self, night: _Night) -> dict[str, Any]:
        """The looks as the cards tell them: each session's state, when it changed and worked."""
        sessions: dict[str, dict[str, Any]] = {}
        seen = blind = False
        blind_since: int | None = None
        for ts, look in night.looks:
            if not look["seen"]:
                blind = True
                blind_since = blind_since or ts
                continue
            seen, blind_since = True, None
            here = set()
            for row in look["sessions"]:
                here.add(row["id"])
                rec = sessions.setdefault(row["id"], {**row, "changed_ms": ts, "trail": []})
                if rec["st"] != row["st"]:
                    rec["changed_ms"] = ts
                rec.update(row)
                self._trail(rec["trail"], ts, busy=bool(row["busy"]))
            for gone in sessions.keys() - here:  # off every list: it is not working
                rec = sessions[gone]
                if rec["busy"] or rec["st"] == "wait":
                    rec.update(st="done", busy=False, changed_ms=ts)
                self._trail(rec["trail"], ts, busy=False)
        for session_id, row in night.live.items():
            if session_id in sessions and blind_since is None:
                sessions[session_id].update(title=row["title"], what=row["what"],
                                            since_ms=row["since_ms"])
        lists: dict[str, bool] = night.looks[-1][1]["lists"] if night.looks else {}
        return {
            "seen": seen,
            "blind": blind,
            "blind_since_ms": blind_since,
            "lists": lists,
            "busy": _tally(night.looks).busy,
            "quiet_ms": _tally(night.looks).quiet_ms,
            "sessions": list(sessions.values()),
        }

    @staticmethod
    def _trail(trail: list[list[int | None]], ts: int, *, busy: bool) -> None:
        if busy and (not trail or trail[-1][1] is not None):
            trail.append([ts, None])
        elif not busy and trail and trail[-1][1] is None:
            trail[-1][1] = ts

    # --- steps: called with the lock held ---------------------------------------

    def _dark_due(self, night: _Night, now: int) -> bool:
        if night.stay_since is not None:
            seen = self._power.presence()
            if seen is None:
                return self._mono() - night.stay_since >= LOOK_IDLE_S
            return seen.locked or seen.idle_s >= LOOK_IDLE_S
        due = night.started_ms + int(DARK_AFTER_S * 1000)
        return now >= due and (not self.busy() or now >= due + _BUSY_WAIT_MS)

    def _darken(self, night: _Night) -> None:
        level = self._power.brightness()
        outputs = self._power.outputs() or {}
        playing = [uid for uid, muted in outputs.items() if muted is False]
        saved = {"brightness": level, "muted": playing}
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
        stays = [uid for uid, muted in outputs.items() if muted is None]
        stays += [uid for uid in playing if not self._power.set_muted(uid, muted=True)]
        if stays or not outputs:
            LOGGER.warning("night run: could not mute %s; its sound stays on",
                           ", ".join(stays) or "the default output")
        self._power.sleep_display()
        night.dark_at = self._mono()

    def _hold(self, night: _Night, now: int) -> None:
        """Let the Mac go when the run is done holding it; renew the assertion while it is not."""
        reason = self._release_reason(night, now)
        if reason is not None:
            self._release(night, reason)
            return
        hold_until = night.hold_until_ms
        renew_before = int(_RENEW_BEFORE_S * 1000)
        if (
            night.assertion is not None
            and hold_until is not None
            and hold_until - now < renew_before
            and _working(_tally(night.looks), now)
        ):
            self._renew(night, now)

    def _release_reason(self, night: _Night, now: int) -> str | None:
        """Why the hold ends now, or None while it lasts."""
        if self._battery_low():
            return "battery"
        if now >= night.started_ms + int(CAP_S * 1000):
            return "cap"
        if now < night.until_ms:
            return None
        tally = _tally(night.looks)
        if not _working(tally, now):
            settled_late = (
                tally.quiet_ms is not None
                and tally.quiet_ms + int(SETTLE_S * 1000) > night.until_ms
            )
            return "settled" if settled_late else "deadline"
        # Work was seen and the lists stopped answering: the deadline alone decides.
        return None if tally.seen else "blind"

    def _renew(self, night: _Night, now: int) -> None:
        """A new assertion first, then the old one goes: no moment without one."""
        until = min(now + int(RENEW_S * 1000), night.started_ms + int(CAP_S * 1000))
        fresh = self._power.hold_awake((until - now) / 1000)
        if fresh is None:
            LOGGER.warning("night run: could not renew the keep-awake; it lapses at its timeout")
            night.hold_until_ms = None
            return
        if night.assertion is not None:
            self._power.release(night.assertion)
        night.assertion, night.hold_until_ms = fresh, until

    def _release(self, night: _Night, reason: str) -> None:
        if night.assertion is not None:
            self._power.release(night.assertion)
            night.assertion = None
        night.released_ms = self._now_ms()
        night.release_reason = reason
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
        self._last = self._last_of(self._fold())
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
        muted = saved.get("muted")
        for uid in muted if isinstance(muted, list) else ():
            if self._power.is_muted(uid) and self._power.set_muted(uid, muted=False):
                restored["volume"] = True
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
        """Every run of the last two weeks, in start order, from its events."""
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
                elif event.type == "night.watched":
                    lists = payload.get("lists")
                    sessions = payload.get("sessions")
                    night.looks.append((event.ts_epoch_ms, {
                        "seen": bool(payload.get("seen")),
                        "lists": dict(lists) if isinstance(lists, Mapping) else {},
                        "sessions": [dict(session) for session in sessions
                                     if isinstance(session, Mapping)]
                        if isinstance(sessions, list) else [],
                    }))
                elif event.type == "night.released":
                    night.released_ms = event.ts_epoch_ms
                    night.release_reason = str(payload.get("reason", ""))
                else:
                    row["ended_ms"] = event.ts_epoch_ms
                    row["reason"] = str(payload.get("reason", ""))
                    restored = payload.get("restored")
                    row["restored"] = dict(restored) if isinstance(restored, Mapping) else {}
        return rows

    def _last_of(self, rows: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
        """The last run that ended, with the totals of the last few nights that held the Mac."""
        ended = [row for row in rows.values() if "ended_ms" in row]
        if not ended:
            return None
        last = ended[-1]
        held = [row for row in ended if row["reason"] != "cancelled"][-_TOTAL_NIGHTS:]
        told = {
            row["night"].night_id: self._summary(
                row["night"], row["ended_ms"], row["reason"], row.get("restored") or {},
            )
            for row in [*held, last]
        }
        summary = told[last["night"].night_id]
        nights = [told[row["night"].night_id]["watch"] for row in held]
        summary["totals"] = {
            "nights": len(nights),
            "extra_ms": sum(watch["extra_ms"] for watch in nights),
            "blind": sum(1 for watch in nights if watch["blind"]),
        }
        return summary

    def _summary(
        self, night: _Night, ended_ms: int, reason: str, restored: Mapping[str, Any],
    ) -> dict[str, Any]:
        """One run as the morning card tells it.

        Beside when the hold went and why, the card compares the two ways it
        could have gone: at the deadline, and when watching alone would have
        let the Mac go (the settle time after the last work seen, or after the
        start when none was; none when work went on to the end). ``extra_ms``
        is how long the deadline held the Mac past that; ``slept_ms`` is when
        the Mac first slept, held or not.
        """
        held_to = night.released_ms or ended_ms
        slept_ms = None
        with self._log() as conn:
            sleeps = iter_events_of_types(conn, ("mac.sleeping",), since_epoch_ms=night.started_ms)
            for event in sleeps:
                if event.ts_epoch_ms < ended_ms:
                    slept_ms = event.ts_epoch_ms
                break
        watch = self._story(night)
        at_end = _tally(night.looks, until_ms=held_to)
        settle = int(SETTLE_S * 1000)
        monitor_ms = (
            None if not watch["seen"] or at_end.busy
            else (at_end.quiet_ms or night.started_ms) + settle
        )
        watch.update(
            monitor_ms=monitor_ms,
            extra_ms=0 if monitor_ms is None
            else max(0, min(night.until_ms, held_to) - monitor_ms),
            busy_at_deadline=_tally(night.looks, until_ms=night.until_ms).busy
            if held_to > night.until_ms else None,
            busy_at_release=at_end.busy,
        )
        return {
            "id": night.night_id,
            "started_ms": night.started_ms,
            "until_ms": night.until_ms,
            "released_ms": night.released_ms,
            "release_reason": night.release_reason,
            "ended_ms": ended_ms,
            "reason": reason,
            "slept_ms": slept_ms,
            "restored": {key: bool(restored.get(key)) for key in ("brightness", "volume")},
            "watch": watch,
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


__all__ = [
    "CAP_S", "DARK_AFTER_S", "SETTLE_S", "WATCH_S", "NightPower", "NightRun", "NightSettings",
    "night_settings",
]
