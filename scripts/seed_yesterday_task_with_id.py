"""S5 fixture variant — seed a task with a caller-specified task_id.

Mirrors ``scripts/seed_yesterday_task.py`` but takes the task_id as
its third positional arg, so S5 can seed two distinct yesterday tasks
and exercise the entity resolver's ambiguity branch.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

from jarvis.deployment import bootstrap_runtime
from jarvis.state.event_log import emit_event, open_event_log


def _yesterday_21_local_epoch_ms() -> int:
    now_local = dt.datetime.now().astimezone()
    yesterday_local = now_local - dt.timedelta(days=1)
    target = yesterday_local.replace(hour=21, minute=0, second=0, microsecond=0)
    return int(target.timestamp() * 1000)


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        sys.stderr.write(
            "usage: seed_yesterday_task_with_id.py REPO_PATH GOAL TASK_ID\n",
        )
        return 2
    repo_path = Path(argv[1]).resolve()
    goal = argv[2]
    task_id = argv[3]

    paths = bootstrap_runtime()
    conn = open_event_log(paths.event_log)
    try:
        ts = _yesterday_21_local_epoch_ms()
        event = emit_event(
            conn,
            type="task.created",
            payload={
                "task_id": task_id,
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
        f"seeded task_id={task_id} event_uid={event.event_uid} goal={goal}\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
