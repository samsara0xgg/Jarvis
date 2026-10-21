r"""Replay the writer of the doing lane (ADR 0302) for past days, against given stores.

    .venv/bin/python -m scripts.doing_replay --date 2026-10-17 [--date 2026-10-18 ...]
        [--from 2026-10-10 --to 2026-10-17] [--dry-run | --brief-only] [--now 2026-10-18T09:00]
        [--event-log PATH] [--memory-db PATH] [--timesink PATH] [--shipped DIR]
        [--config config/jarvis.yaml] [--zone America/Vancouver]

For each day it folds the day line from the event log and memory.db, cuts the waking window,
gathers the clues and the keys of his attention nobody told yet, asks the ``doing.preset`` model
once (twice when its first answer fails a gate; the key comes from the environment, filled from
the runtime root's saved keys as the brain fills it) and prints the gated spans, one per line,
then what it told of the new keys (ADR 0331) and where his attention went in each span by them.
Without ``--dry-run`` the gated spans are appended to ``memory.db`` as the day's newest run (a
final run when the day is over as of ``--now``, else a draft), and what it told with them, exactly
as the daemon's schedule would; with it nothing is stored. The event log is only read: the
model's cost is accounted in a throwaway log that is deleted afterwards. With ``--brief-only`` no
model is asked: each day's input to the writer is printed as it would be sent, with its length.
A day with no waking window or no inputs is skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision.cost_guard import CostRecorder
from jarvis.decision.doing_run import build_messages, inputs_digest
from jarvis.deployment import load_env_file
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime.doing_run import (
    DoingSettings,
    compose_run,
    read_brief,
    store_run,
    store_told,
)
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.session_compaction import build_compact_client
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.day_line import DaySources
from jarvis.state.event_log import open_event_log
from scripts.attention_replay import attention_lines, threads_of, told_lines

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import tzinfo

    from jarvis.decision.llm import LLMClient

REPO = Path(__file__).resolve().parents[1]


def readable(spans: Sequence[Mapping[str, Any]], zone: tzinfo) -> list[str]:
    """One line per span: its times, title, how sure, and the question it asks."""

    def clock(ms: int) -> str:
        return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"

    lines = []
    for span in spans:
        title = span["title"] or "(unknown)"
        line = (
            f"{clock(span['start_ms'])}-{clock(span['end_ms'])} {title} "
            f"[{span['category']}/{span['basis']}/{span['confidence']}]"
        )
        if span["question"] is not None:
            line += f" ? {span['question']['text']} {span['question']['options']}"
        lines.append(line)
    return lines


def replay(  # noqa: PLR0913 — the days, the stores, the knobs, the model and the clock.
    days: Sequence[date], sources: DaySources, event_log: Path, memory_db: Path,
    settings: DoingSettings, client: LLMClient | None, now: datetime, *, dry_run: bool,
    pricing_table: Mapping[str, Mapping[str, float]],
) -> list[dict[str, Any]]:
    """One summary per day, with its gated spans and what it told; stored unless ``dry_run``.

    Without a client no model is asked and each day's summary carries the writer's input instead.
    """
    done: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory() as scratch,
        closing(open_event_log(Path(scratch) / "cost.db")) as cost_log,
    ):
        recorder = CostRecorder(cost_log, pricing_table=pricing_table)
        for day in days:
            brief = read_brief(day, sources, event_log, now)
            line: dict[str, Any] = {"date": day.isoformat()}
            if brief is None or not (brief.facts or brief.clues):
                done.append(line | {"outcome": "skipped (no waking window or no inputs)"})
                continue
            if client is None:
                content = build_messages(brief)[0]["content"]
                done.append(line | {
                    "outcome": "brief only", "segments": len(brief.segments),
                    "clues": len(brief.clues), "new_keys": len(brief.asked),
                    "brief_chars": len(content), "digest": inputs_digest(brief), "brief": content,
                })
                continue
            composed = compose_run(brief, settings, client, recorder)
            line |= {
                "final": brief.final, "segments": len(brief.segments),
                "clues": len(brief.clues), "new_keys": len(brief.asked),
                "digest": inputs_digest(brief), "model": composed.model,
                "tokens_in": composed.tokens_in, "tokens_out": composed.tokens_out,
            }
            told = composed.threads or {}
            names = {one.key: one.name for one in brief.asked}
            said = told_lines(told, names) if composed.threads is not None else [
                f"  not told ({composed.why})",
            ]
            if not dry_run:
                store_told(memory_db, brief, composed)
            if composed.spans is None:
                done.append(line | {"outcome": f"rejected ({composed.why})", "told": said})
                continue
            stored = "dry run" if dry_run else store_run(memory_db, brief, composed)
            done.append(line | {
                "outcome": f"gated ({stored})", "spans": readable(composed.spans, sources.zone),
                "told": said,
                "attention": attention_lines(composed.spans, sources, threads_of(memory_db, told)),
            })
    return done


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
    """Parse the flags, replay the days and print one line each, then the spans."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--date", action="append", type=date.fromisoformat)
    parser.add_argument("--from", dest="first", type=date.fromisoformat)
    parser.add_argument("--to", dest="last", type=date.fromisoformat)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the gated spans, store none")
    mode.add_argument(
        "--brief-only", action="store_true", help="print the writer's input, ask no model",
    )
    parser.add_argument("--now", type=datetime.fromisoformat, help="replay as of this moment")
    root = default_runtime_root()
    parser.add_argument("--event-log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory-db", type=Path, default=root / "memory.db")
    parser.add_argument("--timesink", type=Path, help="a TimeSink store, for his work on the Mac")
    parser.add_argument("--shipped", type=Path, help="the imports directory the Mac shipped to")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    args = parser.parse_args(argv)
    load_env_file(root.expanduser())  # fill-only; the values are never read here
    days = _days(args)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    settings = DoingSettings.from_config(dict(config.get("doing") or {}) | {"enabled": True})
    if settings is None:
        msg = f"doing.prompt in {args.config} is missing"
        raise SystemExit(msg)
    zone = ZoneInfo(args.zone)
    now = args.now or datetime.now(zone)
    sources = DaySources(zone, args.timesink, args.memory_db, shipped=args.shipped)
    table = load_pricing_table(REPO / "data" / "pricing.json")
    client = None if args.brief_only else build_compact_client(
        config.get("llm") or {}, settings.preset,
    )
    for line in replay(
        days, sources, args.event_log, args.memory_db, settings, client,
        now if now.tzinfo else now.replace(tzinfo=zone), dry_run=args.dry_run,
        pricing_table=table,
    ):
        spans = line.pop("spans", [])
        told = line.pop("told", [])
        seen = line.pop("attention", [])
        brief = line.pop("brief", None)
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
        for span in spans:
            sys.stdout.write(f"  {span}\n")
        if told:
            sys.stdout.write("told:\n" + "\n".join(told) + "\n")
        if seen:
            sys.stdout.write("attention:\n" + "\n".join(seen) + "\n")
        if brief is not None:
            sys.stdout.write(brief + "\n")
    if args.dry_run or args.brief_only:
        sys.stdout.write("dry run: nothing was stored\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
