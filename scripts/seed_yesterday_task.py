"""Live-run fixture: emit a backdated `task.created` event.

Used by Deliverable A3 of the ADR-0002 DoD live-run verification. The
canonical flagship utterance refers to "昨天那个 task" (yesterday's
task), but A2 and A3 must run back-to-back on the same calendar day on
Allen's Mac. We seed a `task.created` event at yesterday 21:00 local
time so the L3 time-window resolver can find it.

This script bypasses the normal L3 -> L4 -> task.created flow and calls
`emit_event` directly with an explicit `ts_epoch_ms`. That is acceptable
because the resolver consumes `task.created` from the projection
exclusively — no other event type contributes to `tasks_in_window`.

NOT for production. Only for live-run fixture setup.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from jarvis.deployment import bootstrap_runtime
from jarvis.state.event_log import emit_event, open_event_log


def _yesterday_21_local_epoch_ms() -> int:
    """Yesterday 21:00 in the local timezone -> epoch milliseconds."""
    now_local = dt.datetime.now().astimezone()
    yesterday_local = now_local - dt.timedelta(days=1)
    target = yesterday_local.replace(hour=21, minute=0, second=0, microsecond=0)
    return int(target.timestamp() * 1000)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: seed_yesterday_task.py REPO_PATH [GOAL]\n")
        return 2
    repo_path = Path(argv[1]).resolve()
    goal = argv[2] if len(argv) > 2 else "implement-rate-limiter"

    paths = bootstrap_runtime()
    conn = open_event_log(paths.event_log)
    try:
        ts = _yesterday_21_local_epoch_ms()
        event = emit_event(
            conn,
            type="task.created",
            payload={
                "task_id": "T_yesterday1",
                "goal": goal,
                "source": "fixture",
                "deadline": "today",
                "repo_path": str(repo_path),
                "verify_command": "uv run pytest -x",
            },
            ts_epoch_ms=ts,
        )
    finally:
        conn.close()
    sys.stdout.write(
        f"seeded task.created event_uid={event.event_uid} ts_epoch_ms={ts} "
        f"({dt.datetime.fromtimestamp(ts/1000).astimezone().isoformat()})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
