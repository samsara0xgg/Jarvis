"""ADR 0093 — the night run over a real event log, with the Mac's switches recorded.

The Mac is a recorder (``_Mac``): what the run asked of it is asserted, and its
answers (brightness, sound, presence, battery) are set per step. Its default
output is a Multi-Output Device, as on the owner's Mac: two devices, each
muted on its own. The clock is
the test's: 2026-09-29 23:00 in Vancouver, the morning 06:00. The later checks
take the same run through the Tier 0 rows and tools a spoken request takes,
through the companion's routes, and through the daemon's loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from datetime import time as clock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from jarvis.decision.tier0 import (
    load_tier0_table,
    match_tier0,
    render_tier0_response,
    validate_tier0_table,
)
from jarvis.deployment import bootstrap_runtime
from jarvis.deployment.night_power import Battery, Presence, parse_battery
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime import night_run
from jarvis.runtime.night_run import DIM, NightRun, NightSettings, night_settings
from jarvis.runtime.night_watch import NightWatch, claude_rows, codex_rows, host_rows
from jarvis.shared import ActionRequest, CallerPrincipal, lang
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface import voice_ducking
from jarvis.surface.agent_host import host_sessions
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

ZONE = ZoneInfo("America/Vancouver")
MINUTE = 60_000


def _ms(hour: int, minute: int = 0, *, day: int = 29) -> int:
    """Local time on September ``day``; day 31 is October 1."""
    at = datetime(2026, 9, 1, hour, minute, tzinfo=ZONE) + timedelta(days=day - 1)
    return int(at.timestamp() * 1000)


class _Mac:
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


class _Fixture:
    def __init__(self, tmp_path: Path, *, at: int) -> None:
        self.paths = bootstrap_runtime(tmp_path)
        self.conn = open_event_log(self.paths.event_log)
        self.mac = _Mac()
        self.clock = _Clock(at)
        self.night = self.boot(self.mac)

    def controller(self, mac: _Mac) -> NightRun:
        return NightRun(
            self.paths.event_log, NightSettings(), mac, zone=ZONE,
            now_ms=lambda: self.clock.ms, monotonic=lambda: self.clock.mono,
            sleep=lambda _s: None,
        )

    def boot(self, mac: _Mac) -> NightRun:
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


def _phase(night: NightRun) -> str | None:
    shown = night.snapshot()["night"]
    return None if shown is None else str(shown["phase"])


def _dark(fx: _Fixture) -> str:
    """Start the default run at the fixture's time and let the bedtime seconds pass."""
    fx.night.start(hours=None, until=None, source="companion")
    fx.clock.go(seconds=8)
    fx.night.tick()
    shown = fx.night.snapshot()["night"]
    assert shown["phase"] == "dark"
    return str(shown["id"])


def test_a_run_holds_the_mac_goes_dark_and_a_look_before_morning_stays_a_look(
    tmp_path: Path,
) -> None:
    """Start holds until the deadline; dark comes after the card; a look re-sleeps."""
    fx = _Fixture(tmp_path, at=_ms(23))
    mac, clock, night = fx.mac, fx.clock, fx.night
    reply = night.start(hours=None, until=None, source="companion")
    assert reply["status"] == "started"
    assert reply["until"] == "01:00"
    assert list(mac.held.values()) == [2 * 3600]
    [(etype, started)] = fx.rows()
    assert etype == "night.started"
    assert started["until_ms"] == _ms(1, day=30)
    assert started["wake_at_ms"] == _ms(6, day=30)
    assert started["guarded"] is True
    assert _phase(night) == "starting"

    # The bedtime card's seconds: nothing changes until they are up.
    clock.go(seconds=5)
    night.tick()
    assert not any(mac.mutes.values())
    clock.go(seconds=3)
    night.tick()
    assert fx.rows()[-1] == (
        "night.darkened",
        {"night_id": started["night_id"],
         "saved": {"brightness": 0.8, "muted": ["speakers", "phones"]}},
    )
    assert mac.level == DIM
    assert mac.mutes == {"speakers": True, "phones": True, "tv": False}
    assert mac.calls[-1] == ("display_sleep",)
    assert _phase(night) == "dark"

    # 23:30: the owner unlocks and looks. Before 06:00 that is a look, not getting up.
    clock.go(to=_ms(23, 30))
    mac.seen = Presence(locked=False, idle_s=1.0)
    night.tick()
    assert _phase(night) == "glance"
    assert fx.rows()[-1][0] == "night.darkened"
    assert mac.mutes["speakers"] is True
    # A quiet minute later the display sleeps again; the run goes on.
    clock.go(seconds=61)
    mac.seen = Presence(locked=False, idle_s=62.0)
    night.tick()
    assert mac.calls[-1] == ("display_sleep",)
    assert _phase(night) == "dark"
    fx.close()


def test_the_deadline_lets_only_the_hold_go_and_getting_up_puts_things_back(
    tmp_path: Path,
) -> None:
    """At the deadline the Mac may sleep, still dark; at 07:00 the return restores."""
    fx = _Fixture(tmp_path, at=_ms(23))
    mac, clock, night = fx.mac, fx.clock, fx.night
    night_id = _dark(fx)
    [held_id] = mac.held

    # 01:00, the deadline: only the assertion goes. The Mac then sleeps at 01:05.
    clock.go(to=_ms(1, day=30))
    night.tick()
    assert fx.rows()[-1] == ("night.released", {"night_id": night_id, "reason": "deadline"})
    assert ("release", held_id) in mac.calls
    assert not mac.held
    assert mac.mutes["speakers"] is True
    assert _phase(night) == "dark"
    emit_event(fx.conn, type="mac.sleeping", payload={"ts_epoch_ms": _ms(1, 5, day=30)},
               ts_epoch_ms=_ms(1, 5, day=30))

    # 07:00: input on the unlocked session is getting up.
    clock.go(to=_ms(7, day=30))
    mac.seen = Presence(locked=False, idle_s=2.0)
    night.tick()
    assert fx.rows()[-1] == (
        "night.ended",
        {"night_id": night_id, "reason": "returned",
         "restored": {"brightness": True, "volume": True}},
    )
    assert mac.level == 0.8
    assert not any(mac.mutes.values())
    shown = night.snapshot()
    assert shown["night"] is None
    assert shown["last"] == {
        "id": night_id, "started_ms": _ms(23), "until_ms": _ms(1, day=30),
        "released_ms": _ms(1, day=30), "release_reason": "deadline", "ended_ms": _ms(7, day=30),
        "reason": "returned", "slept_ms": _ms(1, 5, day=30),
        "restored": {"brightness": True, "volume": True},
        # No watch wired: nothing seen, the deadline alone held the Mac.
        "watch": {
            "seen": False, "blind": False, "blind_since_ms": None, "lists": {}, "busy": 0,
            "quiet_ms": None, "sessions": [], "monitor_ms": None, "extra_ms": 0,
            "busy_at_deadline": None, "busy_at_release": 0,
        },
        "totals": {"nights": 1, "extra_ms": 0, "blind": 0},
    }
    fx.close()


def test_what_the_owner_changed_in_the_night_stays_and_a_sleep_while_held_is_reported(
    tmp_path: Path,
) -> None:
    """Only what is still as the run left it comes back; a sleep while held is kept."""
    fx = _Fixture(tmp_path, at=_ms(23))
    mac, clock, night = fx.mac, fx.clock, fx.night
    mac.mutes["phones"] = True  # muted before the run: not the run's to unmute
    night.start(hours=3, until=None, source="conversation", action_id="act-1")
    clock.go(seconds=8)
    night.tick()
    assert fx.rows()[-1][1]["saved"]["muted"] == ["speakers"]
    # The owner turned the speakers back on and the panel up; the lid closed at 23:40.
    mac.mutes["speakers"] = False
    mac.level = 0.5
    emit_event(fx.conn, type="mac.sleeping", payload={"ts_epoch_ms": _ms(23, 40)},
               ts_epoch_ms=_ms(23, 40))
    clock.go(to=_ms(23, 50))
    assert night.end(action_id="act-2") == {"status": "ended", "spoken": lang.t("night.ended")}
    assert mac.mutes == {"speakers": False, "phones": True, "tv": False}
    assert not any(call[0] == "unmute" for call in mac.calls)
    assert mac.level == 0.5
    assert not mac.held
    last = night.snapshot()["last"]
    assert last["restored"] == {"brightness": False, "volume": False}
    assert last["slept_ms"] == _ms(23, 40)
    assert last["reason"] == "ended"

    # Only the panel touched this time, and the output moved to the TV: what the
    # run muted comes back all the same, the TV is left alone, End says just that.
    mac.mutes["phones"] = False
    night.start(hours=None, until=None, source="conversation")
    clock.go(seconds=8)
    night.tick()
    mac.level = 0.9
    mac.default = ("tv",)
    assert night.end() == {"status": "ended", "spoken": lang.t("night.ended_volume")}
    assert mac.mutes == {"speakers": False, "phones": False, "tv": False}
    unmuted = [call for call in mac.calls if call[0] == "unmute"]
    assert unmuted == [("unmute", "speakers"), ("unmute", "phones")]
    assert mac.level == 0.9
    correlations = [
        json.loads(corr) for (corr,) in fx.conn.execute(
            "SELECT correlation_json FROM events WHERE type IN ('night.started', 'night.ended') "
            "AND correlation_json IS NOT NULL ORDER BY id",
        )
    ]
    assert correlations == [{"action_id": "act-1"}, {"action_id": "act-2"}]
    fx.close()


def test_a_restart_picks_the_run_up_and_a_missed_deadline_is_written_down(tmp_path: Path) -> None:
    """A new daemon holds the rest of the run; a deadline it missed is released on boot."""
    fx = _Fixture(tmp_path, at=_ms(23))
    _dark(fx)

    # 23:30: the daemon restarts; the new one holds the rest of the two hours.
    fx.clock.go(to=_ms(23, 30))
    mac = _Mac()
    mac.level = DIM
    mac.mutes.update(speakers=True, phones=True)
    night = fx.boot(mac)
    assert _phase(night) == "dark"
    assert mac.calls == [("hold", 90 * 60)]
    # 07:10, back: what the first daemon found is what comes back.
    fx.clock.go(to=_ms(7, 10, day=30))
    mac.seen = Presence(locked=False, idle_s=1.0)
    night.tick()
    assert [etype for etype, _ in fx.rows()[-2:]] == ["night.released", "night.ended"]
    assert mac.level == 0.8
    assert not any(mac.mutes.values())
    assert not mac.held

    # A run whose deadline passed while no daemon ran gets its release on boot.
    fx.clock.go(to=_ms(22, day=30))
    night.start(hours=1, until=None, source="companion")
    fx.clock.go(to=_ms(23, 30, day=30))
    later = _Mac()
    rebooted = fx.boot(later)
    assert not any(call[0] == "hold" for call in later.calls)
    assert fx.rows()[-1][0] == "night.released"
    assert fx.rows()[-1][1]["reason"] == "deadline"
    assert rebooted.snapshot()["last"]["reason"] == "returned"
    fx.close()


def test_cancel_before_dark_changes_nothing_and_a_daytime_run_ends_at_the_first_return(
    tmp_path: Path,
) -> None:
    """Cancel inside the card's seconds touches nothing; a day run has no looks."""
    fx = _Fixture(tmp_path, at=_ms(14))
    mac, clock, night = fx.mac, fx.clock, fx.night
    night.start(hours=None, until=None, source="companion")
    clock.go(seconds=3)
    assert night.end()["status"] == "cancelled"
    assert fx.rows()[-1][1]["reason"] == "cancelled"
    assert not any(mac.mutes.values())
    assert mac.level == 0.8
    assert not mac.held

    # 14:10 has no morning within reach: the first return after dark is getting up.
    clock.go(to=_ms(14, 10))
    assert night.start(hours=None, until=None, source="companion")["until"] == "16:10"
    assert fx.rows()[-1][1]["wake_at_ms"] is None
    clock.go(seconds=8)
    night.tick()
    clock.go(seconds=3)
    mac.seen = Presence(locked=False, idle_s=0.5)
    night.tick()
    assert _phase(night) == "dark"  # the same visit that started it
    clock.go(seconds=600)
    night.tick()
    assert fx.rows()[-1][1]["reason"] == "returned"
    fx.close()


def test_the_dark_waits_out_the_goodnight_line_and_a_low_battery_lets_the_mac_go(
    tmp_path: Path,
) -> None:
    """Dark waits while a line plays, 20 s past due at most; 9% on battery releases the hold."""
    fx = _Fixture(tmp_path, at=_ms(23))
    mac, clock, night = fx.mac, fx.clock, fx.night
    ducker = voice_ducking.SystemAudioDucker()
    night.busy = lambda: ducker.active or ducker.outputting  # as the daemon wires it
    assert ducker.enter_output(timeout_s=0.0)  # the goodnight line is still playing
    night.start(hours=None, until=None, source="conversation")
    clock.go(seconds=9)
    night.tick()
    assert _phase(night) == "starting"
    ducker.leave_output()
    night.tick()
    assert _phase(night) == "dark"
    mac.cell = Battery(on_battery=True, percent=9)
    clock.go(seconds=120)
    night.tick()
    assert fx.rows()[-1][1]["reason"] == "battery"
    assert not mac.held
    night.end()

    # Output that never stops holds the dark back 20 s past due, no longer.
    assert ducker.enter_output(timeout_s=0.0)
    night.start(hours=None, until=None, source="conversation")
    clock.go(seconds=27)
    night.tick()
    assert _phase(night) == "starting"
    clock.go(seconds=1)
    night.tick()
    assert _phase(night) == "dark"
    fx.close()


def test_an_output_without_a_mute_is_logged_and_the_rest_still_happens(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A display's own speakers take no mute: the log names them; the rest goes on."""
    fx = _Fixture(tmp_path, at=_ms(23))
    mac, night = fx.mac, fx.night
    mac.default = ("speakers", "hdmi")  # the display has no mute, so none in mac.mutes
    with caplog.at_level(logging.WARNING, logger="jarvis.runtime.night_run"):
        night_id = _dark(fx)
    assert "could not mute hdmi; its sound stays on" in caplog.text
    assert fx.rows()[-1] == (
        "night.darkened",
        {"night_id": night_id, "saved": {"brightness": 0.8, "muted": ["speakers"]}},
    )
    assert mac.mutes["speakers"] is True
    assert mac.level == DIM
    assert night.end() == {"status": "ended", "spoken": lang.t("night.ended_restored")}

    # No answer from the sound system at all: said too, and End has only the panel.
    mac.default = None
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="jarvis.runtime.night_run"):
        _dark(fx)
    assert "could not mute the default output" in caplog.text
    assert fx.rows()[-1][1]["saved"] == {"brightness": 0.8, "muted": []}
    assert night.end() == {"status": "ended", "spoken": lang.t("night.ended_brightness")}
    fx.close()


def test_spoken_requests_reach_the_run_through_tier0_and_the_tools(tmp_path: Path) -> None:
    """The owner's phrases hit the night rows; the tools start, refuse and end the run."""
    fx = _Fixture(tmp_path, at=_ms(23))
    registry = build_default_registry(night=fx.night)
    table = load_tier0_table(repo_root() / "config" / "tier0_patterns.yaml")
    regex_tools = registry.for_caller(CallerPrincipal.REGEX_ROUTER)
    validate_tier0_table(
        tuple(row for row in table if row.pattern_id.startswith("night_")),
        allowed_tool_names=frozenset(t.name for t in regex_tools),
        entity_required_tool_names=frozenset(t.name for t in regex_tools if t.requires_entity),
        requires_confirmation_tool_names=frozenset(
            t.name for t in regex_tools if t.requires_confirmation
        ),
    )
    said = {
        "我要睡觉了，让它继续跑": "start_night_run",
        "我去睡了让他继续跑。": "start_night_run",
        "我要睡觉了，能让他继续跑吗？": "start_night_run",
        "我去睡了，你可以让电脑继续跑吗": "start_night_run",
        "挂机两小时": "start_night_run",
        "夜间挂机": "start_night_run",
        "I'm going to bed, keep it running.": "start_night_run",
        "I'm going to sleep, can you keep it running?": "start_night_run",
        "keep it running for two hours": "start_night_run",
        "我起来了。": "end_night_run",
        "结束挂机": "end_night_run",
        "I'm up": "end_night_run",
        "我要睡觉了": None,
        "挂机三小时": None,
    }
    for text, tool_name in said.items():
        hit = match_tier0(text, table)
        assert (hit.tool_name if hit else None) == tool_name, text

    lifecycle = ActionLifecycle()

    def dispatch(name: str, action_id: str, args: dict[str, Any]) -> dict[str, Any]:
        lifecycle.register(action_id)
        lifecycle.transition(action_id, "authorized")
        request = ActionRequest(
            action_id=action_id, tool_name=name, target_entity_ref=None,
            caller_principal=CallerPrincipal.REGEX_ROUTER, risk_level="L1", arguments=args,
            authorization_lease=None, run_id=None, turn_id=None,
        )
        bundle = registry.dispatch(request, fx.conn, fx.paths, lifecycle)
        return dict(bundle.slots[0].payload)

    started = dispatch("start_night_run", "act-start", {})
    assert started["status"] == "started"
    assert started["until"] == "01:00"
    hit = match_tier0("我要睡觉了，让它继续跑", table)
    assert hit is not None
    assert render_tier0_response(hit, started) == started["spoken"]
    assert "8" in started["spoken"]
    assert dispatch("start_night_run", "act-again", {"hours": 5})["status"] == "already"
    assert dispatch("start_night_run", "act-bad", {"hours": 30}) == {"error": "invalid_argument"}
    assert dispatch("end_night_run", "act-end", {})["status"] == "cancelled"
    assert dispatch("end_night_run", "act-none", {})["status"] == "none"
    correlation = fx.conn.execute(
        "SELECT correlation_json FROM events WHERE type = 'night.started'",
    ).fetchone()[0]
    assert json.loads(correlation) == {"action_id": "act-start"}
    fx.close()


def _sess(session_id: str, st: str, **extra: object) -> dict[str, Any]:
    """One row of the agent host's list, as ``GET /events`` sends it."""
    return {"id": session_id, "agent": "claude", "title": f"session {session_id}", "st": st,
            "archived": False, "parked": False, "updated": _ms(22, 30), **extra}


class _Lists:
    """The watch the daemon wires, over lists the test sets per step."""

    def __init__(self) -> None:
        self.host: list[dict[str, Any]] | None = []
        self.codex: dict[str, Any] = {}
        self.looks = 0

    def __call__(self) -> dict[str, list[dict[str, Any]] | None]:
        self.looks += 1
        found: dict[str, list[dict[str, Any]] | None] = {
            "startrail": None if self.host is None else host_rows(self.host),
        }
        if self.codex:
            found["codex"] = codex_rows(self.codex)
        return found


def _watching(fx: _Fixture) -> _Lists:
    lists = _Lists()
    fx.night.watch = lists
    return lists


def _at(fx: _Fixture, hour: int, minute: int = 0, *, day: int = 30) -> None:
    fx.clock.go(to=_ms(hour, minute, day=day))
    fx.night.tick()


def _up(fx: _Fixture, *, day: int = 30) -> dict[str, Any]:
    """07:00, the owner is up: the morning card's run."""
    fx.mac.seen = Presence(locked=False, idle_s=1.0)
    _at(fx, 7, day=day)
    last = fx.night.snapshot()["last"]
    fx.mac.seen = Presence(locked=True, idle_s=9_999.0)
    return dict(last)


def _released(fx: _Fixture) -> list[str]:
    return [payload["reason"] for etype, payload in fx.rows() if etype == "night.released"]


def _bedtime_with_work(fx: _Fixture) -> _Lists:
    """23:00: two sessions at work, one waiting, three the night leaves alone."""
    lists = _watching(fx)
    lists.host = [
        _sess("A", "work", since=_ms(22, 25, day=29), now="在改 night_run.py"),
        _sess("B", "done", bg="1 个后台任务 · 构建"),  # its background build still runs
        _sess("C", "wait", now="要跑 Bash"),
        _sess("D", "done"),  # idle all night: not watched
        _sess("E", "work", archived=True),
        _sess("F", "wait", parked=True),
    ]
    reply = fx.night.start(hours=None, until=None, source="conversation")
    assert reply["working"] == 2
    assert reply["spoken"] == lang.t("night.started_watching", until=lang.spoken_time(
        datetime(2026, 9, 30, 1, 0, tzinfo=ZONE)), seconds=8, working=2)
    [(_, look)] = [row for row in fx.rows() if row[0] == "night.watched"]
    assert look["seen"] is True
    assert look["lists"] == {"startrail": True}
    assert [(row["id"], row["st"], row["busy"]) for row in look["sessions"]] == [
        ("A", "work", True), ("B", "done", True), ("C", "wait", False),
    ]
    shown = fx.night.snapshot()["night"]
    assert shown["cap_ms"] == _ms(11, day=30)
    assert shown["watch"]["busy"] == 2
    assert shown["watch"]["release_ms"] is None  # held while they work
    return lists


def test_a_session_at_work_holds_the_mac_past_the_deadline_until_it_settles(
    tmp_path: Path,
) -> None:
    """Past 01:00 the hold is renewed while A works; 3 minutes after it stops the Mac goes."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    mac, night = fx.mac, fx.night
    lists = _bedtime_with_work(fx)
    [first] = mac.held
    assert mac.held[first] == 2 * 3600

    # 00:31: B's build ends. 00:56, four minutes to the deadline, A still works: renewed.
    host = lists.host or []
    host[1] = _sess("B", "done")
    _at(fx, 0, 31)
    assert night.snapshot()["night"]["watch"]["busy"] == 1
    _at(fx, 0, 56)
    assert first not in mac.held
    [second] = mac.held
    assert mac.held[second] == 15 * 60
    assert mac.calls[-2:] == [("hold", 900), ("release", first)]  # new before old
    _at(fx, 1, 0)
    _at(fx, 1, 8)
    assert _released(fx) == []
    [third] = mac.held
    assert third != second

    # 02:11: A is done. The hold lasts three more minutes, then goes as "settled".
    host[0] = _sess("A", "done", updated=_ms(2, 11, day=30))
    _at(fx, 2, 11)
    assert night.snapshot()["night"]["watch"]["release_ms"] == _ms(2, 14, day=30)
    _at(fx, 2, 13)
    assert _released(fx) == []
    _at(fx, 2, 14)
    assert _released(fx) == ["settled"]
    assert not mac.held
    looks = lists.looks
    _at(fx, 3, 0)
    assert lists.looks == looks  # released: no more looks

    # 07:00, up: the card has both clocks and each session's story.
    last = _up(fx)
    watch = last["watch"]
    assert last["release_reason"] == "settled"
    assert watch["monitor_ms"] == _ms(2, 14, day=30)
    assert watch["busy_at_deadline"] == 1
    assert watch["extra_ms"] == 0
    story = {row["id"]: row for row in watch["sessions"]}
    assert story["A"]["st"] == "done"
    assert story["A"]["changed_ms"] == _ms(2, 11, day=30)
    assert story["A"]["trail"] == [[_ms(23, day=29), _ms(2, 11, day=30)]]
    assert story["B"]["trail"] == [[_ms(23, day=29), _ms(0, 31, day=30)]]
    assert story["C"]["st"] == "wait"
    assert story["C"]["trail"] == []
    assert last["totals"] == {"nights": 1, "extra_ms": 0, "blind": 0}
    fx.close()


def test_the_deadline_holds_when_nothing_works_and_the_morning_counts_the_extra(
    tmp_path: Path,
) -> None:
    """Idle sessions: the Mac goes at 01:00; watching alone would have let it go at 23:03."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    lists = _watching(fx)
    lists.host = [_sess("C", "wait")]
    reply = fx.night.start(hours=None, until=None, source="companion")
    assert reply["spoken"] == lang.t("night.started", until=lang.spoken_time(
        datetime(2026, 9, 30, 1, 0, tzinfo=ZONE)), seconds=8)
    shown = fx.night.snapshot()["night"]
    assert shown["watch"]["release_ms"] == _ms(1, day=30)
    _at(fx, 0, 59)
    assert _released(fx) == []
    _at(fx, 1, 0)
    assert _released(fx) == ["deadline"]
    watch = _up(fx)["watch"]
    assert watch["monitor_ms"] == _ms(23, 3, day=29)
    assert watch["extra_ms"] == _ms(1, day=30) - _ms(23, 3, day=29)

    # A second night, the same: the totals add the two.
    fx.clock.go(to=_ms(23, day=30))
    fx.night.start(hours=1, until=None, source="companion")
    _at(fx, 0, 0, day=31)
    totals = _up(fx, day=31)["totals"]
    assert totals == {"nights": 2, "extra_ms": watch["extra_ms"] + 57 * MINUTE, "blind": 0}
    fx.close()


def test_twelve_hours_is_the_cap_and_a_list_that_goes_quiet_leaves_the_deadline(
    tmp_path: Path,
) -> None:
    """Work that never stops is let go at 11:00; lists that stop answering end the extension."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    lists = _watching(fx)
    lists.codex = {"T": {"session_id": "T", "state": "running", "prompt": "迁移数据",
                         "since_ms": _ms(22, day=29)}}
    fx.night.start(hours=None, until=None, source="companion")
    assert fx.rows()[-1][1]["lists"] == {"codex": True, "startrail": True}
    for hour in range(1, 11):
        _at(fx, hour, 0)
        _at(fx, hour, 30)
    assert max(fx.mac.held.values()) <= 15 * 60
    _at(fx, 10, 58)
    assert fx.mac.held[max(fx.mac.held)] == 2 * 60  # the last renewal stops at the cap
    _at(fx, 11, 0)
    assert _released(fx) == ["cap"]
    fx.mac.seen = Presence(locked=False, idle_s=1.0)
    _at(fx, 11, 5)
    fx.mac.seen = Presence(locked=True, idle_s=9_999.0)
    watch = fx.night.snapshot()["last"]["watch"]
    assert watch["monitor_ms"] is None  # still working when the cap came
    assert watch["busy_at_release"] == 1

    # Past the deadline, the lists stop answering: three missed looks, then the Mac goes.
    fx.clock.go(to=_ms(23, day=30))
    fx.night.start(hours=1, until=None, source="companion")
    _at(fx, 0, 10, day=31)
    lists.host = None
    lists.codex = {}
    _at(fx, 0, 11, day=31)
    _at(fx, 0, 12, day=31)
    assert _released(fx) == ["cap"]
    assert fx.night.snapshot()["night"]["watch"]["seen"] is True
    _at(fx, 0, 13, day=31)
    assert fx.night.snapshot()["night"]["watch"]["blind_since_ms"] == _ms(0, 13, day=31)
    assert _released(fx) == ["cap", "blind"]
    last = _up(fx, day=31)
    assert last["watch"]["blind"] is True
    assert last["totals"]["blind"] == 1
    fx.close()


def test_with_no_list_to_read_the_deadline_alone_decides(tmp_path: Path) -> None:
    """The Agents window is not running: the bedtime card says so, the deadline lets go."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    lists = _watching(fx)
    lists.host = None
    fx.night.start(hours=None, until=None, source="companion")
    watch = fx.night.snapshot()["night"]["watch"]
    assert watch["seen"] is False
    assert watch["lists"] == {"startrail": False}
    _at(fx, 1, 0)
    assert _released(fx) == ["deadline"]
    last = _up(fx)
    assert last["watch"]["monitor_ms"] is None
    assert last["totals"] == {"nights": 1, "extra_ms": 0, "blind": 1}
    fx.close()


def test_a_restart_past_the_deadline_keeps_holding_while_the_last_look_saw_work(
    tmp_path: Path,
) -> None:
    """The new daemon renews for 15 minutes, looks, and lets go once the work has settled."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    lists = _watching(fx)
    lists.host = [_sess("A", "work")]
    _dark(fx)
    _at(fx, 0, 58)
    fx.clock.go(to=_ms(1, 30, day=30))
    mac = _Mac()
    night = fx.boot(mac)
    assert mac.calls == [("hold", 900)]
    assert _released(fx) == []
    night.watch = lists
    lists.host = [_sess("A", "done")]
    night.tick()
    fx.clock.go(to=_ms(1, 33, day=30))
    night.tick()
    assert _released(fx) == ["settled"]
    assert not mac.held
    fx.close()


def test_going_to_answer_a_session_keeps_the_screen_on_until_a_quiet_minute(
    tmp_path: Path,
) -> None:
    """Stay: no dark at the card's 8 s; the screen goes once the owner leaves it a minute."""
    fx = _Fixture(tmp_path, at=_ms(23, day=29))
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        night=fx.night,
    )))
    client.post("/inherent/night", json={"action": "start"})
    fx.clock.go(seconds=3)
    staying = client.post("/inherent/night", json={"action": "stay"}).json()["night"]
    assert staying["stay"] is True
    assert staying["dark_at_ms"] is None
    fx.mac.seen = Presence(locked=False, idle_s=4.0)
    fx.clock.go(seconds=30)
    fx.night.tick()
    assert _phase(fx.night) == "starting"
    fx.mac.seen = Presence(locked=False, idle_s=61.0)
    fx.clock.go(seconds=60)
    fx.night.tick()
    assert _phase(fx.night) == "dark"
    assert fx.mac.mutes["speakers"] is True
    fx.close()


class _Host(BaseHTTPRequestHandler):
    """The agent host's ``GET /events``: a ``hello`` line, then a stream that stays open."""

    key = "k-1"
    hello: bytes = b""

    def do_GET(self) -> None:
        if self.path != "/events" or self.headers.get("Authorization") != f"Bearer {self.key}":
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": hi\n\n" + self.hello + b"\n\n")
        self.wfile.flush()
        time.sleep(0.5)

    def log_message(self, *_args: object) -> None:
        return


def test_the_watch_reads_the_host_claude_code_and_codex_lists() -> None:
    """The host's hello over HTTP; each list folded to one shape; a closed port is None."""
    sessions = [
        _sess("A", "pack"),
        _sess("B", "done", tasks=[{"id": "t", "kind": "local_bash", "what": "构建", "st": "run"}]),
        _sess("C", "err", updated=_ms(1, 7, day=30)),
    ]
    _Host.hello = b"data: " + json.dumps({"t": "hello", "sessions": sessions}).encode()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Host)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        assert host_sessions(port, "k-1") == sessions
        assert host_sessions(port, "wrong") is None
        codex = {"X": {"session_id": "X", "state": "needs_input", "prompt": "改名",
                       "since_ms": _ms(23, day=29)}}
        found = NightWatch(port=port, key=lambda: "k-1", claude=None, codex=codex)()
    finally:
        server.shutdown()
        server.server_close()
    assert [(row["id"], row["st"], row["busy"]) for row in found["startrail"] or []] == [
        ("A", "pack", True), ("B", "done", True), ("C", "err", False),
    ]
    assert found["codex"] == [{"id": "X", "agent": "codex", "title": "改名", "st": "wait",
                               "busy": False, "since_ms": _ms(23, day=29), "what": ""}]
    assert "claude" not in found
    assert host_sessions(port, "k-1", timeout=0.5) is None  # nobody listens any more
    board = {"sessions": [
        {"session_id": "S", "phase": "working", "compacting": True, "title": "t",
         "updated_ms": 1, "activity": "Bash"},
        {"session_id": "T", "phase": "needs_input", "title": "u", "updated_ms": 5},
    ], "error": None}
    assert [(row["id"], row["st"], row["busy"]) for row in claude_rows(board) or []] == [
        ("S", "pack", True), ("T", "wait", False),
    ]
    assert claude_rows({"sessions": [], "error": "claude agents --json: boom"}) is None


def test_the_companion_routes_start_darken_and_end_the_run(tmp_path: Path) -> None:
    """GET draws the run; POST starts, darkens and ends it; bad bodies are 422."""
    fx = _Fixture(tmp_path, at=_ms(23))
    client = TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        night=fx.night,
    )))
    assert client.get("/inherent/night").json() == {
        "night": None, "last": None, "hours": 2.0, "laptop": False,
    }
    shown = client.post("/inherent/night", json={"action": "start", "hours": 1.5}).json()
    assert shown["night"]["phase"] == "starting"
    assert shown["night"]["until_ms"] == _ms(23) + 90 * MINUTE
    assert shown["night"]["dark_at_ms"] == _ms(23) + 8000
    assert shown["laptop"] is True
    darkened = client.post("/inherent/night", json={"action": "dark"}).json()
    assert darkened["night"]["phase"] == "dark"
    assert fx.mac.mutes == {"speakers": True, "phones": True, "tv": False}
    ended = client.post("/inherent/night", json={"action": "end"}).json()
    assert ended["night"] is None
    assert ended["last"]["reason"] == "ended"
    assert not any(fx.mac.mutes.values())
    assert client.post("/inherent/night", json={"action": "nap"}).status_code == 422
    assert client.post("/inherent/night", json={"action": "start", "hours": 20}).status_code == 422
    fx.close()


def test_the_daemon_loop_serves_ticks_and_stops_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run()`` recovers, then serves and ticks; once stopped, a start is refused."""
    monkeypatch.setattr(night_run, "TICK_S", 0.01)
    fx = _Fixture(tmp_path, at=_ms(23))
    night = fx.controller(fx.mac)
    assert night.start(hours=None, until=None, source="conversation")["status"] == "unavailable"

    async def scenario() -> None:
        loop = asyncio.create_task(night.run())
        for _ in range(500):
            await asyncio.sleep(0.01)
            if night.start(hours=None, until=None, source="conversation")["status"] == "started":
                break
        fx.clock.go(seconds=8)
        for _ in range(500):
            await asyncio.sleep(0.01)
            if _phase(night) == "dark":
                break
        loop.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop

    asyncio.run(scenario())
    assert _phase(night) == "dark"
    assert fx.mac.calls[-1] == ("display_sleep",)
    assert night.start(hours=None, until=None, source="conversation")["status"] == "unavailable"
    fx.close()


def test_settings_and_the_macs_own_answers_parse() -> None:
    """``night:`` settings fall back per value; pmset's battery text parses."""
    assert night_settings({"night": {"hours": 3, "morning": "07:30"}}) == NightSettings(
        hours=3.0, morning=clock(7, 30),
    )
    assert night_settings({"night": {"hours": 40, "morning": "late"}}) == NightSettings()
    assert night_settings({}) == NightSettings()
    batteries = {
        "Now drawing from 'AC Power'\n -InternalBattery-0 (id=4653155)\t100%; charged; 0:00 "
        "remaining present: true\n": Battery(on_battery=False, percent=100),
        "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=4653155)\t8%; discharging; "
        "0:31 remaining present: true\n": Battery(on_battery=True, percent=8),
        "Now drawing from 'AC Power'\n": None,
    }
    for text, battery in batteries.items():
        assert parse_battery(text) == battery, text
