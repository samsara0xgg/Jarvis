"""ADR-0018 usage observer — data-driven checks on the event log fold.

Three observable contracts, each asserted on what lands in the log or on
what the read model answers, never on internals:

1. ``emit`` appends ``usage.state_observed`` only for a service whose
   snapshot changed (spec §3.6.1 emit-on-change).
2. ``latest_usage`` answers the newest row per service — the shape
   ``GET /inherent/usage`` serves.
3. The MiniMax estimate folds ``tts.usage_observed`` characters at or
   after the anchor and ignores rows before it.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from jarvis.state.event_log import emit_event, open_event_log
from jarvis.surface.usage_observer import (
    UsageConfig,
    UsageObserver,
    UsageSnapshot,
    estimate_minimax,
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

        # minimax rides every emit as ``unconfigured`` until its anchor exists,
        # but it too is emitted once and then held.
        assert [e.payload["service"] for e in first] == ["claude", "minimax"]
        assert again == []
        assert [e.payload["service"] for e in changed] == ["claude"]

        rows = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = 'usage.state_observed'"
        ).fetchone()
        assert rows == (3,)


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
        assert model["services"]["minimax"]["status"] == "unconfigured"


def test_minimax_estimate_folds_characters_after_anchor(tmp_path: Path) -> None:
    """Characters before the anchor are ignored; the rest price at the unit rate."""
    anchor_ms = 1_000_000
    rows = ((anchor_ms - 1, 5_000), (anchor_ms, 100_000), (anchor_ms + 5, 50_000))
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        for ts, characters in rows:
            emit_event(
                conn,
                type="tts.usage_observed",
                payload={
                    "provider": "minimax",
                    "characters": characters,
                    "response_id": "r",
                    "sequence": 0,
                    "actor": "observer",
                },
                ts_epoch_ms=ts,
            )
        config = UsageConfig(
            minimax_anchor_usd=17.82,
            minimax_anchor_at_ms=anchor_ms,
            minimax_usd_per_million_chars=60.0,
        )
        snapshot = estimate_minimax(conn, config)

    assert snapshot.status == "ok"
    assert snapshot.data["characters_since_anchor"] == 150_000
    # 150k chars x $60/M = $9.00 spent.
    assert snapshot.data["estimate_usd"] == 8.82
