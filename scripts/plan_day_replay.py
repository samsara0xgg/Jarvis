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
(every 2 hours after waking, and when a timed item ends, until ``--day-ends``) goes in two steps as
it does live: a done-check marks the tasks the evidence since the last check shows done (the line
carries ``done_check``: done ids and minutes left), then the budget decides whether the decider is
asked at all: the tasks left, their minutes against the free minutes before ``--day-ends``, and
whether they fit (``budget``, after the check). When they fit the decision is silent.

Without ``--live`` no model is called: a stub decider answers ``tell`` with ``<stub>`` so every
notice shows, and a stub done-check finds nothing done. With ``--live`` each notice (and each
progress check's done-check) goes to the ``day_plan.decide_preset`` model (``haiku`` unless the
config says otherwise; the key comes from the environment). With ``--out`` a markdown
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

from jarvis.decision.day_plan import (
    Decision,
    Progress,
    build_decide_prompt,
    build_progress_prompt,
    parse_decision,
    parse_progress,
    unfit_notice,
)
from jarvis.decision.plan_watch import notice as watch
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime.interview_reminders import local_zone
from jarvis.state import places
from jarvis.state.plan import Decided, Plan, PlanItem, apply_changes, check_item
from jarvis.state.plan_budget import DAY_ENDS, budget
from jarvis.state.plan_facts import evidence, facts_at
from scripts.day_plan_replay import _asker

if TYPE_CHECKING:
    from collections.abc import Callable

    from jarvis.decision.plan_watch import Notice

    type Decide = Callable[[str, str, Plan], Decision]
    type Check = Callable[[str, str, Plan], Progress]

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PRESET = "haiku"
FROM_CLOCK = time(4, 0)
STUB = "<stub>"


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


def stub_check(_system: str, _user: str, _plan: Plan) -> Progress:
    """A done-check that finds nothing done."""
    return Progress()


def live_check(ask: Callable[[str, str], str]) -> Check:
    """A done-check that sends the prompt to the model and parses the answer against the plan."""
    return lambda system, user, held: parse_progress(ask(system, user), held)


def live_decide(ask: Callable[[str, str], str]) -> Decide:
    """A decider that sends the prompt to the model and parses the answer against the plan."""
    return lambda system, user, held: parse_decision(ask(system, user), held)


def _done_check(  # noqa: PLR0913 - the plan, the notice, the log, the moment and the callers.
    held: Plan, one: Notice, conn: sqlite3.Connection, since_ms: int, now: datetime,
    zone: ZoneInfo, check: Check, day_ends: time,
) -> tuple[Plan, Notice, Progress, Notice | None]:
    """``(plan after, notice with its evidence, what the check found, notice to decide on)``."""
    one = replace(one, facts={**one.facts, "evidence": evidence(
        conn, since_ms, int(now.timestamp() * 1000), zone)})
    system, user = build_progress_prompt(held, one, now, zone=zone)
    marked = check(system, user, held)
    held = replace(held, items=tuple(apply_changes(held.items, marked.changes())[0]))
    return held, one, marked, unfit_notice(held, one, marked, now, day_ends)


def replay(  # noqa: PLR0913 - the day, the plan, the stores, the knobs and two callers.
    day: date, plan: Plan, conn: sqlite3.Connection, kept: list[places.Place], zone: ZoneInfo,
    step_min: int, decide: Decide, day_ends: time = DAY_ENDS, check: Check = stub_check,
) -> list[dict[str, Any]]:
    """One line per notice from 04:00 of ``day`` to 04:00 of the next, with its decision.

    A ``progress`` notice goes in two steps as it does live: ``check`` marks what the evidence
    shows done (the line carries ``done_check``), then the decider is asked only when the tasks
    left do not fit the time left.
    """
    held = plan
    seen: set[str] = set()
    told: list[Decided] = []
    lines: list[dict[str, Any]] = []
    last_check: int | None = None
    now = datetime.combine(day, FROM_CLOCK, zone)
    stop = datetime.combine(day + timedelta(days=1), FROM_CLOCK, zone)
    while now < stop:
        now_ms = int(now.timestamp() * 1000)
        facts = facts_at(conn, day, now_ms, zone, kept)
        since_ms = last_check or (facts.wake.at_ms if facts.wake else now_ms)
        for noticed in watch(held, facts, now, seen, day_ends=day_ends):
            one = noticed
            seen.add(one.key)
            extra: dict[str, Any] = {}
            ask: Notice | None = one
            if one.kind == "progress":
                held, one, marked, ask = _done_check(
                    held, one, conn, since_ms, now, zone, check, day_ends)
                last_check = now_ms
                after = budget(held, now, day_ends=day_ends)
                extra = {
                    "done_check": {"done": list(marked.done), "left": marked.left,
                                   "why": marked.why, "notes": list(marked.notes)},
                    "budget": {"remaining_min": after.remaining_min, "free_min": after.free_min,
                               "fits": after.fits},
                }
            if ask is None:
                decision, size = Decision("silent", why="fits"), 0
            else:
                system, user = build_decide_prompt(held, ask, facts, now, told, zone=zone)
                decision, size = decide(system, user, held), len(system) + len(user)
            lines.append({
                "time": f"{now:%H:%M}", "key": one.key, "kind": one.kind,
                "item": next((i.title for i in held.items if i.item_id == one.item_id), None),
                "facts": one.facts, "here": facts.payload(zone)["here"],
                "decision": {
                    "act": decision.act, "say": decision.say, "why": decision.why,
                    "changes": list(decision.changes), "notes": list(decision.notes),
                },
                "prompt_chars": size, **extra,
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
        if "done_check" in line:
            c = line["done_check"]
            out.append(f"  - done-check: done {c['done']}, left {c['left']}")
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
    check: Check = stub_check
    if args.live:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        preset = str((config.get("day_plan") or {}).get("decide_preset") or DEFAULT_PRESET)
        ask = _asker(config, preset)
        decide, check = live_decide(ask), live_check(ask)
    with closing(open_read_only(args.event_log)) as conn:
        lines = replay(
            args.date, plan, conn, kept, zone, args.step_min, decide, args.day_ends, check,
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
