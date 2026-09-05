"""Boot closes playback generations a dead process abandoned (ADR-0008 §4.4)."""

from __future__ import annotations

import contextlib
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from jarvis.runtime import inherent_loop
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.surface.playback_recovery import reconcile_open_playback
from tests.integration.test_conversation_history import _display, _playback

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event

_ORPHAN = ("R", 1)


def _rows(conn: sqlite3.Connection, event_type: str) -> list[Event]:
    """Every row of one type naming the orphan's exact CAS identity."""
    return [
        event
        for event in iter_events(conn)
        if event.type == event_type
        and (event.payload["response_id"], event.payload["playback_generation_id"]) == _ORPHAN
    ]


@pytest.mark.parametrize("checkpointed", [True, False])
def test_boot_reconciler_closes_open_playback_exactly_once(
    tmp_path: Path,
    *,
    checkpointed: bool,
) -> None:
    """One daemon_restart per orphaned generation, idempotent across boots.

    Both cursor branches are exercised: a generation that reached a
    checkpoint carries that cursor forward verbatim, one that never did
    lands on the unheard cursor. The replay fold must accept either.
    """
    with contextlib.closing(open_event_log(tmp_path / "reconcile.db")) as conn:
        _display(conn)
        if checkpointed:
            _playback(conn, "surface.playback_checkpoint", {})
        start = _rows(conn, "surface.playback_started")[-1]
        assert _rows(conn, "surface.playback_interrupted") == []

        first = reconcile_open_playback(conn)
        assert len(first) == 1
        assert first[0].payload["reason"] == "daemon_restart"
        assert first[0].payload["response_id"] == _ORPHAN[0]
        assert first[0].payload["playback_generation_id"] == _ORPHAN[1]
        # PlaybackHistory pins the activation by uid; a different source
        # would flip the fold to inconsistent.
        assert first[0].source_event_id == start.event_uid
        assert "speech_text_hash" not in first[0].payload
        if checkpointed:
            assert first[0].payload["heard_through_sequence"] == 0
            assert first[0].payload["submitted_samples"] == 160
        else:
            assert first[0].payload["heard_through_sequence"] is None
            assert first[0].payload["submitted_samples"] == 0
            assert first[0].payload["heard_text"] == ""

        terminals = _rows(conn, "surface.playback_interrupted")
        assert len(terminals) == 1

        # A second boot over the same orphan appends nothing.
        before = [event.event_uid for event in iter_events(conn)]
        assert reconcile_open_playback(conn) == ()
        assert [event.event_uid for event in iter_events(conn)] == before
        assert len(_rows(conn, "surface.playback_interrupted")) == 1

        # Fold safety: the reconciled log still replays consistent.
        assert fold_conversation_history(iter_events(conn)).consistent


def test_boot_reconciler_skips_a_started_row_without_a_session_id(tmp_path: Path) -> None:
    """One unreadable historical row must not stop the daemon from booting.

    ``terminalize_playback`` requires a non-empty ``session_id`` while the
    registry validates key presence only, so the row is legal to append and
    would otherwise raise straight out of the startup barrier.
    """
    with contextlib.closing(open_event_log(tmp_path / "malformed.db")) as conn:
        _display(conn)
        start = _rows(conn, "surface.playback_started")[-1]
        emit_event(
            conn,
            type="surface.playback_started",
            payload={
                "session_id": None,
                "response_id": "R-malformed",
                "turn_id": "previous",
                "playback_generation_id": 3,
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": hashlib.sha256(b"").hexdigest(),
            },
            source_event_id=start.event_uid,
        )

        closed = reconcile_open_playback(conn)
        assert [event.payload["response_id"] for event in closed] == [_ORPHAN[0]]
        malformed = [
            event
            for event in iter_events(conn)
            if event.type == "surface.playback_interrupted"
            and event.payload["response_id"] == "R-malformed"
        ]
        assert malformed == []


def test_boot_helper_closes_playback_on_its_own_connection(tmp_path: Path) -> None:
    """The daemon's boot helper reconciles without the event-loop's conn.

    ``serve_inherent`` runs on the event-loop thread and ``open_event_log``
    uses ``check_same_thread=True``, so the reconciler has to open its own
    connection — which is what this helper exists for.
    """
    path = tmp_path / "boot.db"
    with contextlib.closing(open_event_log(path)) as conn:
        _display(conn)

    with ThreadPoolExecutor(max_workers=1) as pool:
        closed = pool.submit(
            inherent_loop._reconcile_open_playback_in_thread,  # noqa: SLF001
            path,
            None,
        ).result(timeout=10)
    assert closed == 1

    with contextlib.closing(open_event_log(path)) as conn:
        terminals = _rows(conn, "surface.playback_interrupted")
        assert [event.payload["reason"] for event in terminals] == ["daemon_restart"]
