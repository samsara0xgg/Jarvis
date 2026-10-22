"""What the log says about one turn, or about the turns of a window (ADR 0349).

The runtime asks these questions while it drives, cancels and comments on turns; each function
says what it answers and the SQL stays here, beside the event shapes it reads. The turn id is
the payload's ``turn_id`` unless a function says it reads the correlation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import EVENT_COLUMNS, get_event, row_to_event

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event

# --- where a turn stands ---------------------------------------------------------------------


def turn_has_run(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn already opened a response run, so its answer is out or over."""
    return conn.execute(
        "SELECT 1 FROM events WHERE type = 'response.started' "
        "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def turn_dispatched_action(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn dispatched an action, so it is working on a lookup (read by correlation)."""
    return conn.execute(
        "SELECT 1 FROM events WHERE type = 'action.dispatched' "
        "AND json_extract(correlation_json, '$.turn_id') = ? LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def turn_is_over(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn ended or failed, or its final answer completed, was cancelled or failed.

    A completed answer is a record that cannot be taken back (ADR 0053), so a turn whose final
    run is done is over although no ``turn.ended`` row has landed yet. The turn's own rows are
    read by correlation, its runs by payload.
    """
    return conn.execute(
        "SELECT 1 FROM events WHERE (type IN ('turn.ended', 'turn.failed') "
        "AND json_extract(correlation_json, '$.turn_id') = ?1) "
        "OR (type IN ('response.completed', 'response.cancelled', 'response.failed') "
        "AND json_extract(payload_json, '$.turn_id') = ?1 "
        "AND json_extract(payload_json, '$.response_id') IN ("
        "SELECT json_extract(payload_json, '$.response_id') FROM events "
        "WHERE type = 'response.started' AND json_extract(payload_json, '$.turn_id') = ?1 "
        "AND json_extract(payload_json, '$.phase') = 'final')) LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def turn_ended_or_failed(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether a ``turn.ended`` or ``turn.failed`` row names the turn."""
    return conn.execute(
        "SELECT 1 FROM events WHERE type IN ('turn.ended', 'turn.failed') "
        "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def answer_stopped_by_him(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether his next words, his barge-in or the stop button cancelled the turn's answer.

    That answer will never come. A run cancelled for any other reason does not count: a wait
    line is taken back when the answer starts, and the answer is still owed.
    """
    return conn.execute(
        "SELECT 1 FROM events WHERE type = 'response.cancelled' "
        "AND json_extract(payload_json, '$.reason') IN ('superseded', 'barge_in', 'user_stop') "
        "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def answer_superseded(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether a run of the turn was cancelled because his next sentence took its place."""
    return conn.execute(
        "SELECT 1 FROM events WHERE type = 'response.cancelled' "
        "AND json_extract(payload_json, '$.turn_id') = ? "
        "AND json_extract(payload_json, '$.reason') = 'superseded' LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def turn_cancelled_or_failed(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether a run of the turn was cancelled or failed, or the turn failed (read by payload)."""
    return conn.execute(
        "SELECT 1 FROM events "
        "WHERE type IN ('response.cancelled', 'response.failed', 'turn.failed') "
        "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


def answer_started(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn's answer (any run but a wait line) has opened on a surface or been heard.

    The answer is the first segment's ``surface.response_open`` or ``surface.response_chunk``
    (a streamed turn commits both together, any other turn the whole answer at once), or
    ``surface.playback_started``, the answer reaching the speaker; any of the three counts.
    ``response.started`` does not: it opens the run before the model is asked.
    """
    return conn.execute(
        "SELECT 1 FROM events WHERE type IN "
        "('surface.response_open', 'surface.response_chunk', 'surface.playback_started') "
        "AND json_extract(payload_json, '$.turn_id') = ? "
        "AND json_extract(payload_json, '$.phase') IS NOT 'commentary' LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


_PLAYBACK_ENDS: Final = (
    "surface.playback_completed", "surface.playback_interrupted", "surface.playback_failed",
    "surface.speech_dropped",
)
"""What ends an answer's playback: a lease's terminal, or its never having one (ADR 0106)."""


def playback_ended(conn: sqlite3.Connection, response_id: str) -> bool:
    """Whether the answer ``response_id`` has an end of its playback on the log.

    That is a lease's ``surface.playback_*`` terminal or a ``surface.speech_dropped``, the row for
    an answer let go before any of it played. Without one the next turn reads the answer as heard
    whole.
    """
    return conn.execute(
        "SELECT 1 FROM events WHERE type IN (?, ?, ?, ?) "
        "AND json_extract(payload_json, '$.response_id') = ? LIMIT 1",
        (*_PLAYBACK_ENDS, response_id),
    ).fetchone() is not None


def turn_over_or_answering(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn has ended, had its answer stopped by him, or has started to answer."""
    return (
        turn_ended_or_failed(conn, turn_id)
        or answer_stopped_by_him(conn, turn_id)
        or answer_started(conn, turn_id)
    )


def commentary_runs_opened(conn: sqlite3.Connection, turn_id: str) -> int:
    """How many wait-line (``commentary``) runs the turn has opened.

    ``response.started`` is written first, before the Pre-emit Gate and the surface rows, so it
    is the earliest durable mark of a line said and the only one that beats a second row racing
    in on the next poll. Counting rows rather than watcher memory is what makes a cap on lines
    survive a restart and hold when two rows of one turn open on different worker threads.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE type = 'response.started' "
        "AND json_extract(payload_json, '$.phase') = 'commentary' "
        "AND json_extract(payload_json, '$.turn_id') = ?",
        (turn_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def turn_trigger(conn: sqlite3.Connection, turn_id: str) -> Event | None:
    """The row the turn was claimed from: its first ``turn.started`` names it as its source.

    ``None`` when the turn has no ``turn.started`` or that row names no source.
    """
    started = conn.execute(
        "SELECT source_event_id FROM events WHERE type = 'turn.started' "
        "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id ASC LIMIT 1",
        (turn_id,),
    ).fetchone()
    if started is None or started[0] is None:
        return None
    return get_event(conn, started[0])


def input_channels(conn: sqlite3.Connection, turn_id: str) -> list[str | None]:
    """The channel of each row that carried his words into the turn, in no particular order.

    The rows are ``utterance.received`` (voice) and ``surface.user_intent`` (typed, a card
    answer, a paired device). A row with no channel is ``None``; a turn with no such row gives
    an empty list.
    """
    return [
        channel if isinstance(channel, str) else None
        for (channel,) in conn.execute(
            "SELECT json_extract(payload_json, '$.channel') FROM events "
            "WHERE type IN ('utterance.received', 'surface.user_intent') "
            "AND json_extract(payload_json, '$.turn_id') = ?",
            (turn_id,),
        )
    ]


def latest_input_row(conn: sqlite3.Connection) -> int | None:
    """The log row id of the newest row of his own words, typed or heard; ``None`` if none.

    The echo a plugin connection writes to resume his task (channel ``plugin_resume``) is not
    his words and is left out.
    """
    (latest,) = conn.execute(
        "SELECT MAX(id) FROM events WHERE type IN ('surface.user_intent', 'utterance.received') "
        "AND COALESCE(json_extract(payload_json, '$.channel'), '') != 'plugin_resume'"
    ).fetchone()
    return None if latest is None else int(latest)


def heard_as_barge_pause(conn: sqlite3.Connection, turn_id: str) -> bool:
    """Whether the turn began from a fragment of speech cut at a barge-in pause (ADR 0144)."""
    return conn.execute(
        "SELECT 1 FROM events WHERE type = 'utterance.received' "
        "AND json_extract(correlation_json, '$.turn_id') = ? "
        "AND json_extract(payload_json, '$.endpoint_reason') = 'barge_pause' LIMIT 1",
        (turn_id,),
    ).fetchone() is not None


# --- the turns of a window -------------------------------------------------------------------


def _turn_ids(rows: sqlite3.Cursor) -> set[str]:
    """The non-null turn ids of one column of rows."""
    return {str(turn_id) for (turn_id,) in rows if turn_id is not None}


def turns_of_his_words_since(conn: sqlite3.Connection, *, since_ms: int) -> set[str]:
    """The turns that took his words at or after ``since_ms``.

    A voice sentence (``utterance.received``) or a filled-in ask card (a ``surface.user_intent``
    on the ``clarify`` channel).
    """
    return _turn_ids(conn.execute(
        "SELECT json_extract(payload_json, '$.turn_id') FROM events "
        "WHERE ts_epoch_ms >= ? AND (type = 'utterance.received' "
        "OR (type = 'surface.user_intent' "
        "AND json_extract(payload_json, '$.channel') = 'clarify'))",
        (since_ms,),
    ))


def turns_dispatching_since(conn: sqlite3.Connection, *, since_ms: int) -> set[str]:
    """The turns that dispatched an action at or after ``since_ms`` (read by correlation)."""
    return _turn_ids(conn.execute(
        "SELECT json_extract(correlation_json, '$.turn_id') FROM events "
        "WHERE ts_epoch_ms >= ? AND type = 'action.dispatched'",
        (since_ms,),
    ))


def turns_closed_since(conn: sqlite3.Connection, *, since_ms: int) -> set[str]:
    """The turns that ended, failed or had a run cancelled at or after ``since_ms``.

    Read by correlation.
    """
    return _turn_ids(conn.execute(
        "SELECT json_extract(correlation_json, '$.turn_id') FROM events "
        "WHERE ts_epoch_ms >= ? AND type IN "
        "('turn.ended', 'turn.failed', 'response.cancelled')",
        (since_ms,),
    ))


# --- what a turn put up ----------------------------------------------------------------------


def card_requested_by_turn(conn: sqlite3.Connection, turn_id: str) -> str | None:
    """The id of the confirmation card the turn put up last, or ``None`` if it put up none.

    Whether the card still waits is the pending-confirmation fold's to say; this only names it.
    """
    row = conn.execute(
        "SELECT json_extract(payload_json, '$.confirmation_id') FROM events "
        "WHERE type = 'confirmation.requested' "
        "AND json_extract(correlation_json, '$.turn_id') = ? ORDER BY id DESC LIMIT 1",
        (turn_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def clarifications_asked_in_turn(
    conn: sqlite3.Connection, turn_id: str,
) -> list[tuple[int, Event]]:
    """``(log row id, event)`` of each ask card the turn put up, in the order it put them up."""
    return [
        (int(row[0]), row_to_event(row))
        for row in conn.execute(
            f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant.
            "WHERE type = 'clarification.requested' "
            "AND json_extract(payload_json, '$.turn_id') = ? ORDER BY id",
            (turn_id,),
        )
    ]


def clarification_asked_at(conn: sqlite3.Connection, clarification_id: str) -> int | None:
    """When the ask card was put up, in epoch ms, or ``None`` if no such card was."""
    row = conn.execute(
        "SELECT ts_epoch_ms FROM events WHERE type = 'clarification.requested' "
        "AND json_extract(payload_json, '$.clarification_id') = ?",
        (clarification_id,),
    ).fetchone()
    return None if row is None else int(row[0])


# --- what she said, and how long her turns took -----------------------------------------------


def spoken_since(conn: sqlite3.Connection, *, since_ms: int) -> list[str]:
    """The voice text of each answer segment written at or after ``since_ms``, oldest first.

    The voice text, or the text of a segment that has no separate voice; empty ones are left out.
    """
    return [
        text
        for (text,) in conn.execute(
            "SELECT coalesce(json_extract(payload_json, '$.voice_text'), "
            "json_extract(payload_json, '$.text')) FROM events "
            "WHERE type IN ('surface.response_chunk', 'surface.response_emitted') "
            "AND ts_epoch_ms >= ? ORDER BY id",
            (since_ms,),
        )
        if isinstance(text, str) and text
    ]


def recent_turn_timings(
    conn: sqlite3.Connection, *, since_ms: int, limit: int,
) -> list[Event]:
    """The newest ``limit`` ``turn.timing`` events at or after ``since_ms``, oldest first."""
    rows = conn.execute(
        f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant.
        "WHERE type = 'turn.timing' AND ts_epoch_ms >= ? ORDER BY id DESC LIMIT ?",
        (since_ms, limit),
    ).fetchall()
    return [row_to_event(row) for row in reversed(rows)]
