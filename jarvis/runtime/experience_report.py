"""Print a day's experience table and the weekly line as plain text (ADR 0276).

    python -m jarvis.runtime.experience_report [--date YYYY-MM-DD] [--week]
        [--runtime-root PATH] [--db PATH]

Read-only: it opens memory.db in read mode and reads the rows the nightly schedule stored. No
network, no model, no secrets; it never computes a day. Without ``--date`` it prints the newest
row; ``--week`` adds the most used and most failed labels of the last seven rows.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL
from jarvis.state.day_experience import weekly
from jarvis.state.memory_db import latest_day_experience

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

WEEK_DAYS: Final[int] = 7
_ALL_ROWS: Final[int] = 10_000
_PERCENT: Final[int] = 100


def _share(value: object) -> str:
    return "-" if not isinstance(value, float) else f"{value * _PERCENT:.0f}%"


def _spread(name: str, spread: Mapping[str, Any]) -> str:
    if not spread.get("n"):
        return f"{name}: none"
    return f"{name}: n={spread['n']} median {spread['median']} ms, p90 {spread['p90']} ms"


def format_day(metrics: Mapping[str, Any]) -> list[str]:
    """One day's metrics as plain lines."""
    uses, right = metrics["uses"], metrics["right_first_time"]
    devices = ", ".join(
        f"{node} {n['spoken']} spoken {n['typed']} typed" for node, n in uses["by_device"].items()
    )
    signals = ", ".join(f"{name} {n}" for name, n in right["signals"].items())
    interrupted, proactive = metrics["interrupted_ratio"], metrics["proactive"]
    missed = metrics["hard_missed"]
    return [
        f"{metrics['date']}",
        f"uses: {uses['total']} ({uses['spoken']} spoken, {uses['typed']} typed)"
        + (f" [{devices}]" if devices else ""),
        f"right first time: {right['clean']}/{right['turns']} = {_share(right['share'])}"
        f" (signals counted: {signals})",
        _spread("latency voice", metrics["latency_voice_ms"]),
        _spread("latency typed", metrics["latency_typed_ms"]),
        f"interrupted: {interrupted['interrupted']}/{interrupted['started']} playbacks"
        f" = {_share(interrupted['ratio'])}",
        f"answered unaddressed: {metrics['answered_unaddressed']}",
        f"claimed but not done: {metrics['claimed_not_done']}",
        f"proactive: {proactive['followed']} followed, {proactive['dismissed']} dismissed,"
        f" {proactive['ignored']} ignored",
        f"hard missed: {missed['reminders']} reminders, {missed['rides_missed_alight']} rides"
        " past the stop unwarned",
        f"plan settled: {metrics['plan_settled']}; calendar missed: {metrics['calendar_missed']}",
    ]


def format_week(summary: Mapping[str, Any]) -> str:
    """The weekly line: most used and most failed label."""
    if not summary["turns"]:
        return f"last {summary['days']} days: no labelled voice turns"
    used, failed = summary["most_used"], summary["most_failed"]
    worst = (
        "nothing failed"
        if failed is None
        else f"most failed {failed['label']} ({failed['failed']} of {failed['turns']} not right"
        " the first time)"
    )
    return (
        f"last {summary['days']} days, {summary['turns']} voice turns: most used"
        f" {used['label']} ({used['turns']}), {worst}"
    )


def _root(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser()
    env = os.environ.get("JARVIS_RUNTIME_ROOT")
    return Path(env or DEFAULT_RUNTIME_ROOT_LITERAL).expanduser()


def main(argv: Sequence[str] | None = None) -> int:
    """Print what was asked for; 1 when the store or the row is not there."""
    parser = argparse.ArgumentParser(
        prog="python -m jarvis.runtime.experience_report", description=__doc__,
    )
    parser.add_argument("--date", type=date.fromisoformat, help="the local date (default: newest)")
    parser.add_argument("--week", action="store_true", help="the most used and most failed line")
    parser.add_argument("--runtime-root", type=Path, help="override JARVIS_RUNTIME_ROOT")
    parser.add_argument("--db", type=Path, help="memory.db (default: <runtime root>/memory.db)")
    args = parser.parse_args(argv)
    path = (args.db or _root(args.runtime_root) / "memory.db").resolve()
    if not path.is_file():
        print(f"no memory.db at {path}", file=sys.stderr)  # noqa: T201 - this is the tool's output
        return 1
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        rows = latest_day_experience(conn, _ALL_ROWS if args.date else WEEK_DAYS)
    stored = [(day, json.loads(metrics)) for day, metrics in rows]
    wanted = args.date.isoformat() if args.date else (stored[-1][0] if stored else "")
    found = next((m for day, m in stored if day == wanted), None)
    if found is None:
        print(  # noqa: T201 - this is the tool's output
            f"no experience row for {wanted or 'any day'}", file=sys.stderr,
        )
        return 1
    lines = format_day(found)
    if args.week:
        lines.append(format_week(weekly([m for _day, m in stored[-WEEK_DAYS:]])))
    print("\n".join(lines))  # noqa: T201 - this is the tool's output
    return 0


if __name__ == "__main__":
    sys.exit(main())
