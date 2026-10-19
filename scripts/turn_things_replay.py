r"""Replay the post-turn pass (ADR 0300) over a past stretch, on a scratch copy.

    .venv/bin/python -m scripts.turn_things_replay --event-log EVENTS.db --memory MEMORY.db
        --since 2026-10-13 [--until 2026-10-19] [--live] [--preset haiku-bg]
        [--config config/jarvis.yaml] [--zone America/Vancouver] [--out DIR]

``EVENTS.db`` and ``MEMORY.db`` are copies of the brain's event log and memory.db; the script
refuses any file under the live runtime root and never writes to the files it is given. It builds
scratch copies under ``--out`` (a new temporary directory by default): memory.db copied with the
pass's own rows left out, and an event log that holds only what the pass reads, brought up to each
record's moment as the records are replayed in order, so a record is read against the things, the
plan and the writes of her tool as they stood then. Each record of his in the stretch is read as
the loop reads it once its wait is over; what the pass wrote goes to the scratch copies and is
printed, one JSON line per record: ``{record_id, at, words, asked, verdict, wrote, dropped,
est_input_tokens}``, then a summary line. Without ``--live`` the model is not called (it answers
nothing): the lines show which records would be asked and how large each question is. With
``--live`` each question goes to the preset's real model (the key comes from the environment).
The scratch directory stays for a look.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from collections import deque
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from jarvis.decision import turn_things as decision
from jarvis.deployment.launchd import default_runtime_root
from jarvis.runtime import turn_things as runtime
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.work_state import build_analyst
from jarvis.state import plan, things
from jarvis.state import turn_things as marks
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo

    from jarvis.shared import Event

REPO = Path(__file__).resolve().parents[1]
CHARS_PER_TOKEN = 3.0
"""A rough, high-side guess for mixed English and Chinese."""
_NOTHING = '{"actions": [], "doings": []}'
_SECOND = timedelta(seconds=1)


def refuse_live(*paths: Path) -> None:
    """``SystemExit`` when any path is the live runtime root or under it."""
    root = default_runtime_root().resolve()
    for path in paths:
        if path.resolve().is_relative_to(root):
            msg = (
                f"refusing {path}: it is under the live runtime root {root}; copy the files out "
                "and replay the copies"
            )
            raise SystemExit(msg)


def _asker(config_path: Path, preset: str) -> Callable[[str, str], str]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    analyst = build_analyst(config, preset, pricing_path=None, account_cost=False, kind="replay")
    if analyst is None:
        msg = f"llm.presets.{preset} in {config_path} is missing or malformed"
        raise SystemExit(msg)

    def ask(system: str, user: str) -> str:
        with closing(sqlite3.connect(":memory:")) as conn:  # no accounting: nothing is written
            result = analyst.analyze(
                conn, system=system, messages=[{"role": "user", "content": user}],
                tools=[], tool_choice="none",
            )
        return (result.text or "").strip()

    return ask


def _copy_memory(source: Path, target: Path) -> None:
    """memory.db as it is, without the rows the pass itself left, in a new file."""
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as one, closing(
        sqlite3.connect(target),
    ) as two:
        one.backup(two)
        two.execute("DROP TABLE IF EXISTS turn_things")
        two.commit()


def _ready_at(turn: marks.Turn) -> datetime:
    """The moment the loop would have read the record: her answer in, or the wait over."""
    if turn.answered_at is not None:
        at = turn.answered_at + _SECOND
    else:
        at = turn.at + timedelta(seconds=runtime.ANSWER_WAIT_S)
    if len(turn.notes) < len(turn.pictures):
        at = max(at, (turn.answered_at or turn.at) + timedelta(seconds=runtime.PICTURE_WAIT_S))
    return at


def _copy(log: sqlite3.Connection, event: Event) -> None:
    emit_event(
        log, type=event.type, payload=event.payload, ts_epoch_ms=event.ts_epoch_ms,
        event_uid=event.event_uid,
    )


def _carried(event: Event) -> bool:
    """Whether the event is the one-time carry-in of the older stores (ADR 0299)."""
    return event.type == "thing.carried_in" or event.payload.get("by") == "carry_in"


def replay(  # noqa: PLR0913 - the two stores, the stretch, the model and where the copies go
    event_log: Path, memory: Path, scratch: Path, *, since: datetime, until: datetime,
    zone: tzinfo, ask: Callable[[str, str], str],
) -> list[dict[str, Any]]:
    """Read every record of his in ``[since, until)`` as the loop would; one line each.

    ``ask`` is the model. Both stores are only read; what the pass writes goes under ``scratch``.
    The carry-in of the older stores is in the scratch log from the start, as a first start finds
    it; every other write of the ledger and the plan joins it when its moment comes.
    """
    refuse_live(event_log, memory, scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    copy = scratch / "memory.db"
    _copy_memory(memory, copy)
    settings = runtime.TurnThingsSettings(1, "replay")
    pass_ = runtime.TurnThings(settings, copy, scratch / "events.db", ask, zone)
    marks.queue(copy, since, from_rowid=marks.rowid_since(copy, since))
    with closing(sqlite3.connect(f"file:{event_log}?mode=ro", uri=True)) as source:
        events = [
            event for event in iter_events_of_types(source, (*things.TYPES, *plan.TYPES))
            if event.payload.get("by") != "pass"
        ]
    lines: list[dict[str, Any]] = []
    with closing(open_event_log(scratch / "events.db")) as log:
        for event in events:
            if _carried(event):
                _copy(log, event)
        timeline = deque(event for event in events if not _carried(event))
        for record_id in marks.owed(copy, datetime.now(UTC)):
            turn = marks.read(copy, record_id)
            if turn is None or not since <= turn.at < until:
                continue
            now = _ready_at(turn)
            while timeline and timeline[0].ts_epoch_ms <= int(now.timestamp() * 1000):
                _copy(log, timeline.popleft())
            lines.append(_line(pass_, turn, now))
    return lines


def _line(pass_: runtime.TurnThings, turn: marks.Turn, now: datetime) -> dict[str, Any]:
    """Read one record and say what came of it."""
    line: dict[str, Any] = {
        "record_id": turn.record_id, "at": turn.at.isoformat(timespec="seconds"),
        "words": turn.words[:80], "asked": False, "verdict": "none", "wrote": [], "dropped": [],
    }
    try:
        outcome = pass_.read(turn.record_id, now)
    except runtime.ModelFailed:
        return line | {"asked": True, "verdict": "error"}
    if outcome is not None:
        line |= {
            "asked": bool(outcome.given), "verdict": outcome.verdict, "wrote": list(outcome.wrote),
            "dropped": list(outcome.dropped),
            "est_input_tokens": round((len(decision.INSTRUCTIONS) + len(outcome.given))
                                      / CHARS_PER_TOKEN),
        }
    return line


def main(argv: list[str] | None = None) -> int:
    """Parse the flags, replay the stretch and print one line per record and a summary."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--event-log", type=Path, required=True)
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--until", type=datetime.fromisoformat, default=None)
    parser.add_argument("--live", action="store_true", help="call the preset's real model")
    parser.add_argument("--preset", default="haiku-bg")
    parser.add_argument("--config", type=Path, default=REPO / "config" / "jarvis.yaml")
    parser.add_argument("--zone", default=local_zone())
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    zone = ZoneInfo(args.zone)
    refuse_live(args.event_log, args.memory)
    out = args.out or Path(tempfile.mkdtemp(prefix="turn-things-replay-"))
    since = datetime.combine(args.since.date(), time(), zone)
    until = (
        datetime.combine(args.until.date(), time(), zone) + timedelta(days=1)
        if args.until else datetime.max.replace(tzinfo=UTC)
    )
    ask = _asker(args.config, args.preset) if args.live else (lambda _s, _u: _NOTHING)
    lines = replay(
        args.event_log, args.memory, out, since=since, until=until, zone=zone, ask=ask,
    )
    for line in lines:
        sys.stdout.write(json.dumps(line, ensure_ascii=False) + "\n")
    asked = [line for line in lines if line["asked"]]
    sys.stdout.write(json.dumps({
        "records": len(lines), "asked": len(asked),
        "wrote": sum(len(line["wrote"]) for line in lines),
        "dropped": sum(len(line["dropped"]) for line in lines),
        "est_input_tokens": sum(line.get("est_input_tokens", 0) for line in asked),
        "scratch": str(out),
    }) + "\n")
    if not args.live:
        sys.stdout.write("no model was called (add --live); the lines show what would be asked\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
