r"""Replay the plan watcher (ADR 0289) over a past day, minute by minute, offline.

    .venv/bin/python -m scripts.plan_day_replay --date 2026-10-14 --plan plan.json
        [--step-min 1] [--day-ends 23:30] [--live] [--out DIR] [--event-log PATH] [--memory-db PATH]
        [--config config/jarvis.yaml] [--zone America/Vancouver]

``plan.json`` is ``{"date": "...", "items": [<PlanItem payloads>]}``. The script opens the event
log and memory.db read-only (by default the runtime root's ``mac_events.db`` and ``memory.db``)
and writes nothing to either. From 04:00 of ``--date`` to 04:00 of the next day it asks, every
``--step-min`` minutes, for the facts as the log held them then (``facts_at`` reads only events
timed up to that minute) and for the notices they raise; each notice is printed as one JSON line:
time, key, kind, item, facts and the decision. The decision of a notice that ``adjust``s is
applied to the plan in memory so later notices see it; nothing is stored. A ``progress`` notice
(every 2 hours after waking, and when a timed item ends) shows the budget: the tasks left, their
minutes against the free minutes before ``--day-ends``, and whether they fit.

Without ``--live`` no model is called: a stub decider answers ``tell`` with ``<stub>`` so every
notice shows. With ``--live`` each notice goes to the ``day_plan.decide_preset`` model (``haiku``
unless the config says otherwise; the key comes from the environment). With ``--out`` a markdown
summary ``<out>/<date>.md`` is written. Weather and fresh legs are not fetched in a replay: an
``outing_soon`` notice carries them as None.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision.day_plan import Decision, build_decide_prompt, parse_decision
from jarvis.decision.plan_watch import notice as watch
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime.interview_reminders import local_zone
from jarvis.state import places
from jarvis.state.plan import Decided, Plan, PlanItem, apply_changes, check_item
from jarvis.state.plan_budget import DAY_ENDS
from jarvis.state.plan_facts import facts_at
from scripts.day_plan_replay import _asker

if TYPE_CHECKING:
    from collections.abc import Callable

    type Decide = Callable[[str, str, Plan], Decision]

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PRESET = "haiku"
FROM_CLOCK = time(4, 0)
STUB = "<stub>"


def _budget(facts: dict[str, Any]) -> dict[str, Any]:
    return {k: facts[k] for k in ("remaining_min", "free_min", "fits")}


def load_plan(path: Path, day: date) -> Plan:
    """The plan in ``path`` for ``day``; SystemExit when an item cannot be read."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        items = tuple(PlanItem.from_payload(one) for one in raw["items"])
        for item in items:
            check_item(item)
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"{path}: a plan item does not read ({type(exc).__name__}: {exc})"
        raise SystemExit(msg) from None
    return Plan(day=day, items=items)


def open_read_only(path: Path) -> sqlite3.Connection:
    """``path`` opened so that nothing can be written to it."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def stub_decide(_system: str, _user: str, _plan: Plan) -> Decision:
    """A decider that tells for every notice, so a replay shows them all."""
    return Decision("tell", STUB)


def live_decide(ask: Callable[[str, str], str]) -> Decide:
    """A decider that sends the prompt to the model and parses the answer against the plan."""
    return lambda system, user, held: parse_decision(ask(system, user), held)


def replay(  # noqa: PLR0913 - the day, the plan, the stores, the knobs and the decider.
    day: date, plan: Plan, conn: sqlite3.Connection, kept: list[places.Place], zone: ZoneInfo,
    step_min: int, decide: Decide, day_ends: time = DAY_ENDS,
) -> list[dict[str, Any]]:
    """One line per notice from 04:00 of ``day`` to 04:00 of the next, with its decision."""
    held = plan
    seen: set[str] = set()
    told: list[Decided] = []
    lines: list[dict[str, Any]] = []
    now = datetime.combine(day, FROM_CLOCK, zone)
    stop = datetime.combine(day + timedelta(days=1), FROM_CLOCK, zone)
    while now < stop:
        now_ms = int(now.timestamp() * 1000)
        facts = facts_at(conn, day, now_ms, zone, kept)
        for one in watch(held, facts, now, seen, day_ends=day_ends):
            seen.add(one.key)
            system, user = build_decide_prompt(held, one, facts, now, told, zone=zone)
            decision = decide(system, user, held)
            lines.append({
                "time": f"{now:%H:%M}", "key": one.key, "kind": one.kind,
                "item": next((i.title for i in held.items if i.item_id == one.item_id), None),
                "facts": one.facts, "here": facts.payload(zone)["here"],
                "decision": {
                    "act": decision.act, "say": decision.say, "why": decision.why,
                    "changes": list(decision.changes), "notes": list(decision.notes),
                },
                "prompt_chars": len(system) + len(user),
                **({"budget": _budget(one.facts)} if one.kind == "progress" else {}),
            })
            if decision.act != "silent":
                told.append(Decided(one.key, decision.act, decision.say, decision.why,
                                    "replay", now_ms))
            if decision.act == "adjust":
                held = replace(held, items=tuple(apply_changes(held.items, decision.changes)[0]))
        now += timedelta(minutes=step_min)
    return lines


def summary(day: date, lines: list[dict[str, Any]]) -> str:
    """The replay as markdown: one block per notice."""
    out = [f"# Plan watch replay {day.isoformat()} {day.strftime('%A')}", ""]
    for line in lines:
        what = f" ({line['item']})" if line["item"] else ""
        decision = line["decision"]
        out.append(f"- {line['time']} {line['kind']}{what}: {decision['act']}")
        if "budget" in line:
            b = line["budget"]
            out.append(f"  - budget: {b['remaining_min']} min of tasks, {b['free_min']} free, "
                       + ("fits" if b["fits"] else "does not fit"))
        if decision["say"]:
            out.append(f"  - say: {decision['say']}")
        if decision["why"]:
            out.append(f"  - why: {decision['why']}")
        if decision["changes"]:
            out.append(f"  - changes: {json.dumps(decision['changes'], ensure_ascii=False)}")
        if decision["notes"]:
            out.append(f"  - dropped: {'; '.join(decision['notes'])}")
    if not lines:
        out.append("No notices.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Parse the flags, replay the day and print one JSON line per notice."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else "")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--plan", required=True, type=Path, help="a JSON file of the day's plan")
    parser.add_argument("--step-min", type=int, default=1)
    parser.add_argument("--day-ends", type=time.fromisoformat, default=DAY_ENDS)
    parser.add_argument("--live", action="store_true", help="call the decide preset's real model")
    parser.add_argument("--out", type=Path, help="write <out>/<date>.md")
    root = default_runtime_root()
    parser.add_argument("--event-log", type=Path, default=root / "mac_events.db")
    parser.add_argument("--memory-db", type=Path, default=root / "memory.db")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    args = parser.parse_args(argv)
    if args.step_min < 1:
        parser.error("--step-min must be at least 1")
    zone = ZoneInfo(args.zone)
    plan = load_plan(args.plan, args.date)
    kept = places.read_kept(args.memory_db)
    decide: Decide = stub_decide
    if args.live:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        preset = str((config.get("day_plan") or {}).get("decide_preset") or DEFAULT_PRESET)
        decide = live_decide(_asker(config, preset))
    with closing(open_read_only(args.event_log)) as conn:
        lines = replay(
            args.date, plan, conn, kept, zone, args.step_min, decide, args.day_ends,
        )
    for line in lines:
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    if args.out is not None:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / f"{args.date.isoformat()}.md").write_text(
            summary(args.date, lines), encoding="utf-8",
        )
    if not args.live:
        sys.stdout.write("no model was called (add --live)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
