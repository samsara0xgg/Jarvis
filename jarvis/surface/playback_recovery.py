"""Close playback generations a previous process abandoned (ADR-0008 §4.4).

The live in-process playback actor is the only writer of a playback terminal
while a daemon is up, so a process killed mid-playback leaves the generation
permanently open in the log.  This is the boot half of that ownership: it runs
once inside the startup barrier, on its own connection, with no live actor,
and reconstructs every payload field from the log alone.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Final

from jarvis.shared.realtime import TerminalCommitted
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.lifecycle_terminal import terminalize_playback

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus

_PLAYBACK_STARTED: Final[str] = "surface.playback_started"
_PLAYBACK_CHECKPOINT: Final[str] = "surface.playback_checkpoint"
_PLAYBACK_INTERRUPTED: Final[str] = "surface.playback_interrupted"
_PLAYBACK_TERMINALS: Final[tuple[str, ...]] = (
    "surface.playback_completed",
    _PLAYBACK_INTERRUPTED,
    "surface.playback_failed",
)
# Carried verbatim off the last checkpoint so the replay fold sees the cursor
# it already accepted.  ``speech_text_hash`` is deliberately absent:
# ``PlaybackHistory._cursor`` compares it against the folded speech hash.
_CURSOR_FIELDS: Final[tuple[str, ...]] = (
    "heard_through_sequence",
    "submitted_samples",
    "heard_text_hash",
    "heard_text",
)
_UNHEARD_CURSOR: Final[dict[str, object]] = {
    "heard_through_sequence": None,
    "submitted_samples": 0,
    "heard_text": "",
    "heard_text_hash": hashlib.sha256(b"").hexdigest(),
}


def _cas_identity(event: Event) -> tuple[str, int] | None:
    """Return the ``(response_id, playback_generation_id)`` CAS identity.

    ``session_id`` and ``turn_id`` ride on the payloads but are not part of
    the identity :func:`~jarvis.state.lifecycle_terminal.terminalize_playback`
    arbitrates on.  A row that cannot name a legal identity is skipped rather
    than crashing boot.
    """
    response_id = event.payload.get("response_id")
    generation = event.payload.get("playback_generation_id")
    if (
        not isinstance(response_id, str)
        or not response_id
        or type(generation) is not int
        or generation < 0
    ):
        return None
    return (response_id, generation)


def reconcile_open_playback(
    conn: sqlite3.Connection,
    *,
    committed_event_bus: CommittedEventBus | None = None,
) -> tuple[Event, ...]:
    """Close every open playback generation once as ``daemon_restart``.

    Idempotent by construction: only starts with no terminal for their exact
    ``(response_id, playback_generation_id)`` pair are closed, and the
    playback CAS refuses a second terminal, so a repeated boot appends
    nothing and returns an empty tuple.
    """
    started: dict[tuple[str, int], Event] = {}
    cursors: dict[tuple[str, int], dict[str, object]] = {}
    terminated: set[tuple[str, int]] = set()
    for event in iter_events_of_types(
        conn,
        (_PLAYBACK_STARTED, _PLAYBACK_CHECKPOINT, *_PLAYBACK_TERMINALS),
    ):
        identity = _cas_identity(event)
        if identity is None:
            continue
        if event.type == _PLAYBACK_STARTED:
            started[identity] = event
            cursors.pop(identity, None)
        elif event.type == _PLAYBACK_CHECKPOINT:
            cursors[identity] = {
                field: event.payload[field]
                for field in _CURSOR_FIELDS
                if field in event.payload
            }
        else:
            terminated.add(identity)

    closed: list[Event] = []
    for identity, start in started.items():
        if identity in terminated:
            continue
        response_id, generation = identity
        turn_id = str(start.payload.get("turn_id", ""))
        payload: dict[str, object] = {
            "session_id": start.payload.get("session_id"),
            "response_id": response_id,
            "turn_id": turn_id,
            "playback_generation_id": generation,
            **cursors.get(identity, _UNHEARD_CURSOR),
            "reason": "daemon_restart",
        }
        outcome = terminalize_playback(
            conn,
            event_type=_PLAYBACK_INTERRUPTED,
            payload=payload,
            # PlaybackHistory pins its activation to the started row's uid and
            # flips ``consistent`` for any later row naming a different source.
            source_event_id=start.event_uid,
            correlation={"turn_id": turn_id},
            committed_event_bus=committed_event_bus,
        )
        if isinstance(outcome, TerminalCommitted):
            closed.append(outcome.event)
    return tuple(closed)


__all__ = ["reconcile_open_playback"]
