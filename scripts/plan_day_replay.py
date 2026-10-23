r"""Replay the plan watcher (ADR 0383) over a past day, minute by minute, offline.

    .venv/bin/python -m scripts.plan_day_replay --day 2026-10-14
        [--log PATH] [--memory PATH] [--zone America/Vancouver] [--live PRESET]
        [--config config/jarvis.yaml]

The event log and memory.db (by default the runtime root's ``mac_events.db`` and ``memory.db``)
are copied to a temporary directory first; the watcher runs on the copies and nothing is ever
written to the given files. From 04:00 of ``--day`` to 04:00 of the next day the real watcher is
ticked once a minute at an injected time, over the plan the log holds for that day (the script
needs one), and every line it says is printed: time, kind, key and text. At the end it prints how
many lines of each kind and how many in all.

Without ``--live`` no model is called: the morning and the evening line are a fixed ``<stub>``
message. With ``--live PRESET`` they go to that preset's real model (the key comes from the
environment). Weather and fresh legs are not fetched in a replay, and nothing is pushed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import yaml

from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime import day_plan as runtime
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.plan_run import PlanWatch
from jarvis.state import plan
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import open_runtime_event_log
from scripts.day_plan_replay import _asker

if TYPE_CHECKING:
    from collections.abc import Callable

REPO = Path(__file__).resolve().parents[1]
FROM_CLOCK = (4, 0)
STUB = "<stub>"


def stub_ask(_system: str, _user: str) -> str:
    """A model that answers every line with a fixed message, so a replay shows them all."""
    return json.dumps({"message": STUB})


def copy_of(source: Path, target: Path) -> Path:
    """A consistent copy of the sqlite file ``source`` at ``target``; ``source`` is read-only."""
    with (
        closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as read,
        closing(sqlite3.connect(target)) as write,
    ):
        read.backup(write)
    return target


def replay(
    log: Path, memory: Path, day: date, zone: ZoneInfo, ask: Callable[[str, str], str],
) -> list[tuple[str, str, str, str]]:
    """``(time, kind, key, text)`` of each line the watcher says on ``day``, in order.

    Runs on copies of ``log`` and ``memory``; ``log`` must hold a plan (or a failed draft) for
    ``day``, else SystemExit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        events, memory_copy = copy_of(log, root / "events.db"), copy_of(memory, root / "memory.db")
        sources = runtime.DayPlanSources(
            events, memory_copy, zone, DaySources(zone, None, memory_copy),
        )
        watch = PlanWatch(
            settings=runtime.DayPlanSettings(watch=True), sources=sources, ask=ask,
        )
        with closing(open_runtime_event_log(events)) as conn:
            if plan.for_day(conn, day) is None and plan.failed(conn, day) is None:
                msg = f"{log}: no plan for {day} in the log"
                raise SystemExit(msg)
            lines: list[tuple[str, str, str, str]] = []
            now = datetime.combine(day, datetime.min.time(), zone).replace(hour=FROM_CLOCK[0])
            stop = now + timedelta(days=1)
            while now < stop:
                for key in watch.tick(now):
                    one = plan.said(conn, day)[key]
                    lines.append((f"{now:%H:%M}", one.kind, one.key, one.text))
                now += timedelta(minutes=1)
    return lines


def summary(lines: list[tuple[str, str, str, str]]) -> str:
    """How many lines of each kind, and in all."""
    kinds = Counter(kind for _, kind, _, _ in lines)
    parts = [f"{kind} {kinds[kind]}" for kind in plan.KINDS]
    return f"{', '.join(parts)}; total {len(lines)}"


def main(argv: list[str] | None = None) -> int:
    """Parse the flags, replay the day and print each line and the summary."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    root = default_runtime_root()
    parser.add_argument("--log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory", type=Path, default=root / "memory.db")
    parser.add_argument("--zone", default=local_zone())
    parser.add_argument("--live", metavar="PRESET", help="call this preset's real model")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    args = parser.parse_args(argv)
    ask: Callable[[str, str], str] = stub_ask
    if args.live:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        ask = _asker(config, args.live)
    lines = replay(args.log, args.memory, args.day, ZoneInfo(args.zone), ask)
    for at, kind, key, text in lines:
        sys.stdout.write(f"{at} {kind} {key} {text}\n")
    sys.stdout.write(summary(lines) + "\n")
    if not args.live:
        sys.stdout.write("no model was called (add --live PRESET)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
