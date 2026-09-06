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
from jarvis.surface import voice_media, voice_tts
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
    from collections.abc import Callable
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


def _whole(
    conn: sqlite3.Connection,
    *,
    response_id: str,
    group_id: str,
    turn_id: str,
    text: str,
) -> list[tuple[int, Event]]:
    """Emit one complete response: open, chunk, emitted."""
    identity = {"response_id": response_id, "group_id": group_id, "turn_id": turn_id}
    return _emit_open(conn, text=text, **identity) + _emit_emitted(
        conn, text=text, **identity,
    )


def _declined(response_id: str) -> list[dict[str, object]]:
    return [
        dict(point.attributes)
        for point in realtime_trace_snapshot()
        if point.name == "media_foreground_declined"
        and point.attributes.get("response_id") == response_id
    ]


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


def _trace(name: str, response_id: str) -> list[dict[str, object]]:
    return [
        dict(point.attributes)
        for point in realtime_trace_snapshot()
        if point.name == name and point.attributes.get("response_id") == response_id
    ]


def _held_terminal(
    response_id: str,
    actions: list[str],
) -> tuple[Callable[..., object], threading.Event]:
    """Fault one terminal append so the incumbent stays tombstoned for a retry."""
    original = terminalize_playback
    tombstoned = threading.Event()

    def _faulted(*args: object, **kwargs: object) -> object:
        payload = cast("dict[str, object]", kwargs["payload"])
        if payload["response_id"] == response_id and not tombstoned.is_set():
            tombstoned.set()
            msg = "injected playback terminal append fault"
            raise RuntimeError(msg)
        outcome = original(*cast("Any", args), **cast("Any", kwargs))
        if payload["response_id"] == response_id:
            actions.append(f"terminal-durable:{response_id}")
        return outcome

    return _faulted, tombstoned


def _debt_pipeline(
    db_path: Path,
    provider: _FakeProvider,
    player: voice_tts.AudioStreamPlayer,
) -> voice_media.StreamingTTSPipeline:
    return voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        # A first faulted terminal append leaves the incumbent tombstoned for a
        # whole retry delay; that is the draining-lane branch under test.
        config=replace(_config(), durability_retry_s=0.5),
        foreground_decision_callable=decide_foreground,
        start_player=False,
    )


def test_draining_lane_is_granted_to_a_cross_group_winner_and_purged(
    tmp_path: Path,
) -> None:
    """A tombstoned incumbent is draining, not occupying: the winner speaks."""
    db_path = tmp_path / "foreground-grant.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RDEBT", 0): _Behavior("success", final_delay_s=0.01)},
        candidate_count=1,
    )
    player = _player()
    pipeline = _debt_pipeline(db_path, provider, player)
    faulted, tombstoned = _held_terminal("RDEBT", provider.actions)
    reset_realtime_trace()
    try:
        with (
            patch.object(voice_media, "terminalize_playback", side_effect=faulted),
            _CallbackPump(player),
        ):
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RDEBT", group_id="GDEBT", turn_id="TDEBT",
                text="incumbent answer",
            )))
            assert tombstoned.wait(timeout=2.0)
            # Same group: the incumbent's own continuation queues behind it.
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RCONT", group_id="GDEBT", turn_id="TDEBT",
                text="same group continuation",
            )))
            # Newer group with a later row id: takes the draining lane.
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RNEXT", group_id="GNEXT", turn_id="TNEXT",
                text="cross group answer",
            )))
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()
    assert ("RNEXT", 0) in provider.opened
    assert ("RCONT", 0) not in provider.opened
    terminals = _terminal_rows(open_event_log(db_path))
    terminal_kind = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert terminal_kind == {
        "RDEBT": "surface.playback_completed",
        "RNEXT": "surface.playback_completed",
    }
    assert provider.actions.index("terminal-durable:RDEBT") < provider.actions.index(
        "open:RNEXT",
    )
    granted = _trace("media_foreground_draining_lane_granted", "RNEXT")
    assert [attributes["purged_lane_depth"] for attributes in granted] == [1]


def test_draining_lane_still_declines_a_cross_group_loser(tmp_path: Path) -> None:
    """The grant is policy, not a hole: an older group loses the draining lane too."""
    db_path = tmp_path / "foreground-grant-loser.db"
    conn = open_event_log(db_path)
    provider = _FakeProvider(
        {("RDEBT", 0): _Behavior("success", final_delay_s=0.01)},
        candidate_count=1,
    )
    player = _player()
    pipeline = _debt_pipeline(db_path, provider, player)
    faulted, tombstoned = _held_terminal("RDEBT", provider.actions)
    reset_realtime_trace()
    try:
        with (
            patch.object(voice_media, "terminalize_playback", side_effect=faulted),
            _CallbackPump(player),
        ):
            # The straggler opens FIRST (lower row id) and emits LAST.
            _emit_open(
                conn,
                response_id="ROLD",
                group_id="GOLD",
                turn_id="TOLD",
                text="stale answer",
            )
            asyncio.run(_submit_response(pipeline, _whole(
                conn, response_id="RDEBT", group_id="GDEBT", turn_id="TDEBT",
                text="incumbent answer",
            )))
            assert tombstoned.wait(timeout=2.0)
            asyncio.run(_submit_response(pipeline, _emit_emitted(
                conn,
                response_id="ROLD",
                group_id="GOLD",
                turn_id="TOLD",
                text="stale answer",
            )))
            assert pipeline.wait_until_idle(timeout_s=3.0)
    finally:
        assert pipeline.close(wait_timeout_s=2.0)
        conn.close()
    assert ("ROLD", 0) not in provider.opened
    terminals = _terminal_rows(open_event_log(db_path))
    terminal_kind = {str(payload["response_id"]): kind for kind, payload in terminals}
    assert terminal_kind == {"RDEBT": "surface.playback_completed"}
    declined = _declined("ROLD")
    assert [attributes["verdict"] for attributes in declined] == ["decline"]
    assert declined[0]["terminal_commit_pending"] is True
