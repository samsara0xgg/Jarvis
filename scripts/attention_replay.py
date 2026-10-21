r"""Show where his attention went in each stretch of past days (ADR 0314), and tell keys (0315).

    .venv/bin/python -m scripts.attention_replay --date 2026-10-19 [--date ...]
        [--tell [--store] [--threads-prompt FILE]] [--now 2026-10-20T12:00]
        [--event-log PATH] [--memory-db PATH] [--shipped DIR]
        [--config config/jarvis.yaml] [--zone America/Vancouver]

For each day it serves the doing lane as ``GET /inherent/day`` does and prints, for every doing
stretch, its time and title, then where his attention went in it: each thread with its minutes,
typing and keys, the rows, and what was alongside. No model is asked and nothing is written.

With ``--tell`` the day's keys that nobody told yet are first asked of the ``doing.threads_preset``
model (else ``doing.preset``) with ``doing.threads_prompt``, or the prompt in ``--threads-prompt``
(the key comes from the environment, filled from the runtime root's saved keys as the brain fills
it). What it told is printed and the stretches are shown with it; only with ``--store`` is it
appended to memory.db as the brain would. The model's cost is accounted in a throwaway log.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision.cost_guard import CostRecorder
from jarvis.deployment import load_env_file
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime.daemon.routes import read_day
from jarvis.runtime.doing_run import (
    DoingSettings,
    compose_threads,
    read_brief,
    read_thread_brief,
)
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.shared.pricing import load_pricing_table
from jarvis.state import attention, imports
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import append_attention_threads, attention_threads, open_memory_db

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import tzinfo

REPO = Path(__file__).resolve().parents[1]
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


def show(  # noqa: PLR0913 — the day, its stores and the clock, and what was told on top.
    day: date, sources: DaySources, event_log: Path, memory_db: Path, now: datetime,
    told: Mapping[str, tuple[str | None, str | None]],
) -> list[str]:
    """Each doing stretch of ``day`` as the page serves it, with ``told`` laid over the store."""
    page = read_day(
        event_log, sources.zone, memory_db, None, day, int(now.timestamp() * 1000),
        shipped=sources.shipped,
    )
    with closing(open_memory_db(memory_db)) as conn:
        threads = {k: attention.Thread(*v) for k, v in {**attention_threads(conn), **told}.items()}
    lines = []
    for span in page["spans"]:
        if span["lane"] != "doing" or span["kind"] == "planned":
            continue
        found = attention.read(sources, span["start_ms"], span["end_ms"])
        within = attention.within(found, span["start_ms"], span["end_ms"], threads)
        lines += stretch_lines(span, within, sources.zone)
    return lines


def tell(  # noqa: PLR0913 — the day, its stores, the knobs, the model and the clock.
    day: date, sources: DaySources, event_log: Path, memory_db: Path, settings: DoingSettings,
    config: Mapping[str, Any], now: datetime, *, store: bool,
) -> tuple[dict[str, tuple[str | None, str | None]], list[str]]:
    """Ask what ``day``'s untold keys served; what was told and what to print about it."""
    brief = read_brief(day, sources, event_log, now)
    if brief is None:
        return {}, ["no waking window"]
    asked = read_thread_brief(brief, sources, memory_db)
    if asked is None:
        return {}, ["no key to tell"]
    client = build_compact_client(
        config.get("llm") or {}, settings.threads_preset or settings.preset,
    )
    table = load_pricing_table(REPO / "data" / "pricing.json")
    with (
        tempfile.TemporaryDirectory() as scratch,
        closing(open_event_log(Path(scratch) / "cost.db")) as cost_log,
    ):
        recorder = CostRecorder(cost_log, pricing_table=table)
        told = compose_threads(asked, settings, client, recorder)
    spent = f"model {told.model}, {told.tokens_in} in / {told.tokens_out} out"
    if told.threads is None:
        return {}, [f"rejected ({told.why}), {spent}"]
    if store:
        append_attention_threads(
            memory_db, told.threads, origin="model", model=told.model, day=brief.day,
        )
    names = {one.key: one.name for one in asked.asked}
    head = f"told {len(told.threads)} of {len(asked.asked)} keys, {spent}"
    return told.threads, [
        head + (", stored" if store else ", not stored"), *told_lines(told.threads, names),
    ]


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
    """Parse the flags; for each day tell its keys if asked, then print its stretches."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--date", action="append", type=date.fromisoformat)
    parser.add_argument("--from", dest="first", type=date.fromisoformat)
    parser.add_argument("--to", dest="last", type=date.fromisoformat)
    parser.add_argument("--tell", action="store_true", help="ask a model what new keys served")
    parser.add_argument("--store", action="store_true", help="keep what it told in memory.db")
    parser.add_argument("--threads-prompt", type=Path, help="a prompt file instead of the config's")
    parser.add_argument("--now", type=datetime.fromisoformat, help="replay as of this moment")
    root = default_runtime_root()
    parser.add_argument("--event-log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory-db", type=Path, default=root / "memory.db")
    parser.add_argument("--shipped", type=Path, default=root / imports.IMPORTS_DIRNAME)
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    args = parser.parse_args(argv)
    if args.store and not args.tell:
        msg = "--store goes with --tell"
        raise SystemExit(msg)
    days = _days(args)
    zone = ZoneInfo(args.zone)
    now = args.now or datetime.now(zone)
    now = now if now.tzinfo else now.replace(tzinfo=zone)
    sources = DaySources(zone, None, args.memory_db, shipped=args.shipped)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    raw = dict(config.get("doing") or {}) | {"enabled": True}
    if args.threads_prompt is not None:
        raw["threads_prompt"] = args.threads_prompt.read_text(encoding="utf-8")
    settings = DoingSettings.from_config(raw)
    if args.tell:
        load_env_file(root.expanduser())  # fill-only; the values are never read here
        if settings is None or settings.threads_prompt is None:
            msg = "--tell needs doing.threads_prompt or --threads-prompt"
            raise SystemExit(msg)
    for day in days:
        sys.stdout.write(f"== {day.isoformat()}\n")
        told: dict[str, tuple[str | None, str | None]] = {}
        if args.tell and settings is not None:
            told, said = tell(
                day, sources, args.event_log, args.memory_db, settings, config, now,
                store=args.store,
            )
            sys.stdout.write("\n".join(said) + "\n")
        lines = show(day, sources, args.event_log, args.memory_db, now, told)
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
