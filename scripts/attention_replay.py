r"""Show where his attention went in each stretch of past days (ADR 0330).

    .venv/bin/python -m scripts.attention_replay --date 2026-10-19 [--date ...]
        [--now 2026-10-20T12:00] [--event-log PATH] [--memory-db PATH] [--shipped DIR]
        [--zone America/Vancouver]

For each day it serves the doing lane as ``GET /inherent/day`` does and prints, for every doing
stretch, its time and title, then where his attention went in it: each thread with its minutes,
typing and keys, the rows, and what was alongside, by the keys told so far. No model is asked and
nothing is written; ``scripts.doing_replay`` has the writer tell new keys (ADR 0331).
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime.inherent_loop import _read_day
from jarvis.runtime.interview_reminders import local_zone
from jarvis.state import attention, imports
from jarvis.state.day_line import DaySources
from jarvis.state.memory_db import attention_threads, open_memory_db

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import tzinfo

_MIN = 60_000


def told_lines(
    told: Mapping[str, tuple[str | None, str | None]], names: Mapping[str, str],
) -> list[str]:
    """One line per key told: its name, and its thread or that it goes with what is around it."""
    return [
        f"  {names.get(key, key)} -> "
        + ("(what is around it)" if thread is None else f"{thread} [{category}]")
        for key, (thread, category) in sorted(told.items(), key=lambda kv: (kv[1][0] or "", kv[0]))
    ]


def stretch_lines(span: Mapping[str, Any], told: Mapping[str, Any], zone: tzinfo) -> list[str]:
    """A stretch's head line, then its threads, rows and what was alongside."""

    def clock(ms: int) -> str:
        return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"

    def minutes(ms: float) -> str:
        return f"{round(ms / _MIN)}m"

    lines = [
        f"{clock(span['start_ms'])}-{clock(span['end_ms'])} {span['title'] or '(unknown)'} "
        f"| front {minutes(told['front_ms'])}",
    ]
    names = {t["id"]: t["name"] for t in told["threads"]}
    for t in told["threads"]:
        typed = f" typed {round(t['typed_ms'] / 1000)}s" if t["typed_ms"] else ""
        keys = ", ".join(f"{k['name']} {minutes(k['ms'])}" for k in t["keys"][:4])
        head = f"  {t['name']} [{t['category'] or '-'}] {t['where']} {minutes(t['ms'])}"
        lines.append(f"{head}{typed}: {keys}")
    lines += [
        f"  row {clock(r['start_ms'])}-{clock(r['end_ms'])} {names.get(r['thread'], r['thread'])} "
        f"{minutes(r['ms'])}, others {minutes(r['others_ms'])}, {r['switches']} switches"
        for r in told["rows"]
    ]
    lines += [
        f"  alongside {a['name']} {a['where']} {minutes(a['ms'])}" for a in told["alongside"]
    ]
    return lines


def threads_of(
    memory_db: Path, told: Mapping[str, tuple[str | None, str | None]],
) -> dict[str, attention.Thread]:
    """The keys told in memory.db, with ``told`` laid over them."""
    with closing(open_memory_db(memory_db)) as conn:
        return {k: attention.Thread(*v) for k, v in {**attention_threads(conn), **told}.items()}


def attention_lines(
    spans: Iterable[Mapping[str, Any]], sources: DaySources,
    threads: Mapping[str, attention.Thread],
) -> list[str]:
    """Each doing stretch of ``spans`` with where his attention went in it, by ``threads``."""
    lines = []
    for span in spans:
        if span.get("lane", "doing") != "doing" or span["kind"] == "planned":
            continue
        found = attention.read(sources, span["start_ms"], span["end_ms"])
        within = attention.within(found, span["start_ms"], span["end_ms"], threads)
        lines += stretch_lines(span, within, sources.zone)
    return lines


def show(
    day: date, sources: DaySources, event_log: Path, memory_db: Path, now: datetime,
) -> list[str]:
    """Each doing stretch of ``day`` as the page serves it, by the keys told in memory.db."""
    page = _read_day(
        event_log, sources.zone, memory_db, None, day, int(now.timestamp() * 1000),
        shipped=sources.shipped,
    )
    return attention_lines(page["spans"], sources, threads_of(memory_db, {}))


def _days(args: argparse.Namespace) -> list[date]:
    found = set(args.date or [])
    if args.first or args.last:
        if not (args.first and args.last):
            msg = "--from and --to go together"
            raise SystemExit(msg)
        found.update(
            args.first + timedelta(days=n) for n in range((args.last - args.first).days + 1)
        )
    if not found:
        msg = "name the days: --date, or --from and --to"
        raise SystemExit(msg)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    """Parse the flags; for each day print its stretches."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--date", action="append", type=date.fromisoformat)
    parser.add_argument("--from", dest="first", type=date.fromisoformat)
    parser.add_argument("--to", dest="last", type=date.fromisoformat)
    parser.add_argument("--now", type=datetime.fromisoformat, help="replay as of this moment")
    root = default_runtime_root()
    parser.add_argument("--event-log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory-db", type=Path, default=root / "memory.db")
    parser.add_argument("--shipped", type=Path, default=root / imports.IMPORTS_DIRNAME)
    parser.add_argument("--zone", default=local_zone())
    args = parser.parse_args(argv)
    days = _days(args)
    zone = ZoneInfo(args.zone)
    now = args.now or datetime.now(zone)
    now = now if now.tzinfo else now.replace(tzinfo=zone)
    sources = DaySources(zone, None, args.memory_db, shipped=args.shipped)
    for day in days:
        sys.stdout.write(f"== {day.isoformat()}\n")
        lines = show(day, sources, args.event_log, args.memory_db, now)
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
