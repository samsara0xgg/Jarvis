"""Cross-group foreground lane arbitration (ADR-0008 D8, ADR-0006 drain lane).

The lane policy is a pure L3 function injected into L5 through the runtime;
these cases pin the two dispositions that used to be unconditional: an older
cross-group straggler now declines instead of cutting off the newer answer,
and a cross-group candidate never queues behind a tombstoned incumbent.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

from jarvis.decision.response_run import decide_foreground
from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback
from jarvis.surface import voice_media
from tests.integration.test_wave2_streaming_media import (
    _Behavior,
    _CallbackPump,
    _config,
    _FakeProvider,
    _player,
    _submit_response,
    _terminal_rows,
)

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path
    from typing import Any

    from jarvis.shared import Event


def _emit(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    payload: dict[str, object],
) -> list[tuple[int, Event]]:
    """Emit one response event and keep its Event Log row id."""
    event = emit_event(conn, type=event_type, payload=payload)
    row = conn.execute(
        "SELECT id FROM events WHERE event_uid = ?",
        (event.event_uid,),
    ).fetchone()
    assert row is not None
    return [(int(row[0]), event)]


def _identity(response_id: str, group_id: str, turn_id: str) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "response_id": response_id,
        "response_group_id": group_id,
        "phase": "final",
        "channel": "speech",
    }


def _emit_open(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit open+chunk only; the buffer's row id is the open's row id."""
    base = _identity(response_id, group_id, turn_id)
    return _emit(
        conn,
        event_type="surface.response_open",
        payload={**base, "query": "q", "kind": "text"},
    ) + _emit(
        conn,
        event_type="surface.response_chunk",
        payload={
            **base,
            "text": text,
            "sequence": 0,
            "segment_hash": hashlib.sha256(text.encode()).hexdigest(),
        },
    )


def _emit_emitted(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit the terminal response event that schedules the buffer."""
    return _emit(
        conn,
        event_type="surface.response_emitted",
        payload={**_identity(response_id, group_id, turn_id), "text": text},
    )


def _declined(response_id: str) -> list[dict[str, object]]:
    return [
        dict(point.attributes)
        for point in realtime_trace_snapshot()
        if point.name == "media_foreground_declined"
        and point.attributes.get("response_id") == response_id
    ]


def test_decide_foreground_orders_the_lane_by_event_log_commit_order() -> None:
    """Same group queues; a cross-group candidate wins only by later row id."""
    assert decide_foreground("G1", 5, "G1", 4) == "enqueue_after_drain"
    assert decide_foreground("G1", 5, "G2", 6) == "supersede"
    assert decide_foreground("G1", 5, "G2", 4) == "decline"
    assert decide_foreground("G1", 5, "G2", 5) == "decline"


def test_older_cross_group_straggler_declines_instead_of_superseding(
    tmp_path: Path,
) -> None:
    """A late answer from an older turn no longer cuts off the newer answer."""
    db_path = tmp_path / "foreground-order.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RNEW", 0): _Behavior("success", final_delay_s=0.01)},
        candidate_count=1,
    )
    gate = threading.Event()
    provider.segment_gates[("RNEW", 0)] = gate
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=_config(),
        foreground_decision_callable=decide_foreground,
        start_player=False,
    )
    reset_realtime_trace()
    try:
        with _CallbackPump(player):
            # The straggler opens FIRST (lower row id) and emits LAST.
            _emit_open(
                conn,
                response_id="ROLD",
                group_id="GOLD",
                turn_id="TOLD",
                text="stale answer",
            )
            newer = _emit_open(
                conn,
                response_id="RNEW",
                group_id="GNEW",
                turn_id="TNEW",
                text="fresh answer",
            ) + _emit_emitted(
                conn,
                response_id="RNEW",
                group_id="GNEW",
                turn_id="TNEW",
                text="fresh answer",
            )
            older = _emit_emitted(
                conn,
                response_id="ROLD",
                group_id="GOLD",
                turn_id="TOLD",
                text="stale answer",
            )
            asyncio.run(_submit_response(pipeline, newer))
            asyncio.run(_submit_response(pipeline, older))
            gate.set()
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close()
        conn.close()
    assert ("ROLD", 0) not in provider.opened
    terminals = _terminal_rows(open_event_log(db_path))
    terminal_kind = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert terminal_kind == {"RNEW": "surface.playback_completed"}
    assert [attributes["verdict"] for attributes in _declined("ROLD")] == ["decline"]


def test_tombstoned_incumbent_never_queues_a_cross_group_candidate(
    tmp_path: Path,
) -> None:
    """A cross-group winner declines rather than entering the same-group drain lane."""
    db_path = tmp_path / "foreground-debt.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RDEBT", 0): _Behavior("success", final_delay_s=0.01)},
        candidate_count=1,
    )
    player = _player()
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        # A first faulted terminal append leaves the incumbent tombstoned for
        # a whole retry delay; that is the branch the candidate must not queue on.
        config=replace(_config(), durability_retry_s=0.5),
        foreground_decision_callable=decide_foreground,
        start_player=False,
    )
    original_terminalize = terminalize_playback
    tombstoned = threading.Event()

    def _faulted_terminalize(*args: object, **kwargs: object) -> object:
        payload = cast("dict[str, object]", kwargs["payload"])
        if payload["response_id"] == "RDEBT" and not tombstoned.is_set():
            tombstoned.set()
            msg = "injected playback terminal append fault"
            raise RuntimeError(msg)
        return original_terminalize(*cast("Any", args), **cast("Any", kwargs))

    reset_realtime_trace()
    try:
        with (
            patch.object(
                voice_media,
                "terminalize_playback",
                side_effect=_faulted_terminalize,
            ),
            _CallbackPump(player),
        ):
            incumbent = _emit_open(
                conn,
                response_id="RDEBT",
                group_id="GDEBT",
                turn_id="TDEBT",
                text="incumbent answer",
            ) + _emit_emitted(
                conn,
                response_id="RDEBT",
                group_id="GDEBT",
                turn_id="TDEBT",
                text="incumbent answer",
            )
            asyncio.run(_submit_response(pipeline, incumbent))
            assert tombstoned.wait(timeout=2.0)
            candidate = _emit_open(
                conn,
                response_id="RNEXT",
                group_id="GNEXT",
                turn_id="TNEXT",
                text="cross group answer",
            ) + _emit_emitted(
                conn,
                response_id="RNEXT",
                group_id="GNEXT",
                turn_id="TNEXT",
                text="cross group answer",
            )
            asyncio.run(_submit_response(pipeline, candidate))
            assert pipeline.wait_until_idle(timeout_s=2.0)
    finally:
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()
    assert ("RNEXT", 0) not in provider.opened
    terminals = _terminal_rows(open_event_log(db_path))
    terminal_kind = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert terminal_kind == {"RDEBT": "surface.playback_completed"}
    assert not [
        point
        for point in realtime_trace_snapshot()
        if point.name == "media_terminal_debt_wait_enqueued"
    ]
    declined = _declined("RNEXT")
    assert [attributes["verdict"] for attributes in declined] == ["supersede"]
    assert declined[0]["terminal_commit_pending"] is True
