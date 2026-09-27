"""ADR-0018 usage observer — data-driven checks on the event log fold.

Two observable contracts, each asserted on what lands in the log or on
what the read model answers, never on internals:

1. ``emit`` appends ``usage.state_observed`` only for a service whose
   snapshot changed (spec §3.6.1 emit-on-change).
2. ``latest_usage`` answers the newest row per service — the shape
   ``GET /inherent/usage`` serves.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from jarvis.state.event_log import open_event_log
from jarvis.surface.usage_observer import (
    UsageConfig,
    UsageObserver,
    UsageSnapshot,
    latest_usage,
)

if TYPE_CHECKING:
    from pathlib import Path


def _claude(percent: float) -> UsageSnapshot:
    window = {"key": "five_hour", "label": "5 小时", "percent": percent, "resets_at": None}
    return UsageSnapshot("claude", "ok", {"plan": "20X", "windows": [window]})


def test_emit_only_on_change(tmp_path: Path) -> None:
    """An unchanged snapshot appends nothing; a changed one appends one row."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        observer = UsageObserver(conn, UsageConfig())
        first = observer.emit([_claude(10.0)])
        again = observer.emit([_claude(10.0)])
        changed = observer.emit([_claude(38.0)])

        assert [e.payload["service"] for e in first] == ["claude"]
        assert again == []
        assert [e.payload["service"] for e in changed] == ["claude"]

        rows = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'usage.state_observed'"
        ).fetchone()
        assert rows == (2,)


def test_latest_usage_answers_newest_row_per_service(tmp_path: Path) -> None:
    """The read model folds the newest row per service; restart recovers the baseline."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        observer = UsageObserver(conn, UsageConfig())
        observer.emit([_claude(10.0)])
        observer.emit([_claude(38.0)])

        # A restart recovers the baseline from the log: the same snapshot
        # after recovery is still "unchanged".
        restarted = UsageObserver(conn, UsageConfig())
        restarted.recover_baselines()
        assert restarted.emit([_claude(38.0)]) == []

        model = latest_usage(conn)
        claude = model["services"]["claude"]
        assert claude["status"] == "ok"
        assert claude["data"]["windows"][0]["percent"] == 38.0
