r"""Replay the day-plan draft (ADR 0288) for past days, offline.

    .venv/bin/python -m scripts.day_plan_replay --date 2026-10-10 --date 2026-10-11
        [--live] [--out DIR] [--event-log PATH] [--memory-db PATH]
        [--config config/jarvis.yaml] [--zone America/Vancouver]

For each ``--date`` it builds the inputs as the runtime would at that day's ``day_plan.at``,
reading the event log and memory.db (by default the runtime root's ``mac_events.db`` and
``memory.db``; nothing is written to either), and prints the input sizes and an estimated cost
from ``data/pricing.json``. The things come from the log; the weather needs the daemon's live
connection, so a replay notes it as missing. Without ``--live`` no model is called and no file is
written. With ``--live`` the day's prompt goes to the ``day_plan.preset`` model (the key comes from
the environment) and ``<out>/<date>.json`` (the parsed items, the dropped notes) and
``<out>/<date>.md`` (a readable day list) are written. Nothing else is written anywhere.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision.day_plan import build_prompt, parse_items
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime import day_plan as runtime
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.work_state import build_analyst
from jarvis.shared.pricing import load_pricing_table
from jarvis.state.day_line import DaySources

if TYPE_CHECKING:
    from jarvis.state.plan import PlanItem

REPO = Path(__file__).resolve().parents[1]
CHARS_PER_TOKEN = 3.0
"""A rough, high-side guess for mixed English and Chinese JSON."""
OUTPUT_TOKENS = 3000
"""What a day's answer is assumed to cost to write, thinking included."""


def sizes(system: str, user: str) -> dict[str, int]:
    """Characters and estimated tokens of one prompt."""
    chars = len(system) + len(user)
    return {
        "system_chars": len(system), "user_chars": len(user),
        "est_input_tokens": round(chars / CHARS_PER_TOKEN),
    }


def estimate_cost(tokens_in: int, model: str, table: dict[str, dict[str, float]]) -> float | None:
    """USD for ``tokens_in`` and the assumed output; None when the model has no price."""
    entry = table.get(model)
    if entry is None:
        return None
    return (tokens_in * entry["input"] + OUTPUT_TOKENS * entry["output"]) / 1_000_000


def day_list(day: date, items: list[PlanItem]) -> str:
    """A readable day: time, title, place, why and what to prepare, one item per line."""
    lines = [f"# {day.isoformat()} {day.strftime('%A')}", ""]
    for item in items:
        if item.start:
            stop = (int(item.start[:2]) * 60 + int(item.start[3:]) + item.minutes) % 1440
            when = f"{item.start}-{stop // 60:02d}:{stop % 60:02d}"
        elif item.window_from or item.window_to:
            when = f"{item.window_from or '--:--'}-{item.window_to or '--:--'} (window)"
        else:
            when = "--:--"
        mark = " [private]" if item.private else ""
        place = f" @ {item.place}" if item.place else ""
        lines.append(f"- {when} {item.title}{place} ({item.firmness}){mark}")
        if item.why:
            lines.append(f"  - why: {item.why}")
        if item.prepare:
            lines.append(f"  - prepare: {', '.join(item.prepare)}")
    return "\n".join(lines) + "\n"


def _asker(config: dict[str, Any], preset: str) -> runtime.Ask:
    analyst = build_analyst(config, preset, pricing_path=None, account_cost=False, kind="replay")
    if analyst is None:
        msg = f"llm.presets.{preset} is missing or malformed"
        raise SystemExit(msg)

    def ask(system: str, user: str) -> str:
        with closing(sqlite3.connect(":memory:")) as conn:  # no accounting: nothing is written
            result = analyst.analyze(
                conn, system=system, messages=[{"role": "user", "content": user}],
                tools=[], tool_choice="none",
            )
        return (result.text or "").strip()

    return ask


def replay(  # noqa: PLR0913 - the days, the stores, the knobs and where the files go.
    days: list[date], sources: runtime.DayPlanSources, settings: runtime.DayPlanSettings,
    model: str, table: dict[str, dict[str, float]], ask: runtime.Ask | None, out: Path | None,
) -> list[dict[str, Any]]:
    """One summary line per day; with ``ask`` the model is called and the files are written."""
    lines: list[dict[str, Any]] = []
    for day in days:
        at = datetime.combine(day, settings.at, sources.zone)
        inputs = runtime.gather(sources, settings, day, at)
        system, user = build_prompt(inputs, zone=sources.zone, today=day)
        cost = estimate_cost(sizes(system, user)["est_input_tokens"], model, table)
        line: dict[str, Any] = {
            "date": day.isoformat(), **sizes(system, user), "est_cost_usd": cost,
            "fixed": len(inputs.fixed), "goals": len(inputs.goals),
            "his_changes": len(inputs.his_changes), "carried": len(inputs.carried),
            "missing": list(inputs.notes), "asked": False,
        }
        if ask is not None and out is not None:
            items, notes = parse_items(
                ask(system, user), known_sources=inputs.sources, fixed=inputs.fixed,
            )
            out.mkdir(parents=True, exist_ok=True)
            body = {
                "date": day.isoformat(), "preset": settings.preset, "digest": inputs.digest(),
                "notes": notes, "items": [item.payload() for item in items],
            }
            (out / f"{day.isoformat()}.json").write_text(
                json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
            (out / f"{day.isoformat()}.md").write_text(day_list(day, items), encoding="utf-8")
            line |= {"asked": True, "items": len(items), "dropped": len(notes)}
        lines.append(line)
    return lines


def main(argv: list[str] | None = None) -> int:
    """Parse the flags, replay the days and print one line each."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--date", action="append", required=True, type=date.fromisoformat)
    parser.add_argument("--live", action="store_true", help="call the preset's real model")
    parser.add_argument("--out", type=Path, default=Path("day-plan-replay"))
    root = default_runtime_root()
    parser.add_argument("--event-log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory-db", type=Path, default=root / "memory.db")
    parser.add_argument("--timesink", type=Path, help="a TimeSink store, for his work on the Mac")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    args = parser.parse_args(argv)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    block = dict(config.get("day_plan") or {}) | {"enabled": True}
    settings = runtime.DayPlanSettings.from_config(block)
    if settings is None:
        msg = f"day_plan.at in {args.config} is not HH:MM"
        raise SystemExit(msg)
    zone = ZoneInfo(args.zone)
    sources = runtime.DayPlanSources(
        args.event_log, args.memory_db, zone, DaySources(zone, args.timesink, args.memory_db),
    )
    presets = (config.get("llm") or {}).get("presets") or {}
    model = str((presets.get(settings.preset) or {}).get("model") or "")
    table = load_pricing_table(REPO / "data" / "pricing.json")
    ask = _asker(config, settings.preset) if args.live else None
    for line in replay(args.date, sources, settings, model, table, ask, args.out):
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    if not args.live:
        sys.stdout.write("no model was called and no file was written (add --live)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
