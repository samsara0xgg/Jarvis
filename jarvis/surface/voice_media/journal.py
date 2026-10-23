"""The media owner's Event Log connection and every row it writes or reads for playback."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sqlite3
from typing import TYPE_CHECKING

from jarvis.state.event_log import EVENT_COLUMNS, emit_event, row_to_event, turn_intent_channel
from jarvis.state.lifecycle_terminal import terminalize_playback

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from jarvis.shared import Event
    from jarvis.surface.voice_ledger import OutputTimelineSnapshot
    from jarvis.surface.voice_media.response import ActiveResponse

LOGGER = logging.getLogger(__name__)

_SELECT_EVENT_ROWS_THROUGH = (
    f"SELECT {EVENT_COLUMNS} FROM events "  # noqa: S608 - the columns are a constant.
    "WHERE id > ? AND id <= ? ORDER BY id ASC LIMIT ?"
)


class PlaybackJournal:
    """The only code that touches the media owner's connection; the actor's thread only (ADR 0383).

    The connection is opened on the actor's thread and every call runs there, so a call never
    yields to another task and its writes keep their order against the owner's other steps.
    The owner decides when a row is written; the journal says how it is written and read.
    """

    def __init__(self, conn_factory: Callable[[], sqlite3.Connection]) -> None:
        """Open nothing until the actor asks: ``conn_factory`` is called by ``open``."""
        self._conn_factory = conn_factory
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        """Open the actor's connection."""
        self._conn = self._conn_factory()

    def close(self) -> None:
        """Close the connection if one was opened; a failing close is ignored."""
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        conn = self._conn
        if conn is None:
            msg = "media owner Event Log connection is unavailable"
            raise RuntimeError(msg)
        return conn

    def event_rows_through(
        self, after_id: int, through_id: int, limit: int,
    ) -> Iterator[tuple[int, Event]]:
        """The events with ``after_id < id <= through_id`` in id order, at most ``limit``.

        The rows are read when the first one is asked for and each is decoded as it is
        reached, so a row that cannot be decoded is met after the rows before it were handled.
        """
        conn = self._require_conn()
        rows = conn.execute(
            _SELECT_EVENT_ROWS_THROUGH,
            (
                after_id,
                through_id,
                limit,
            ),
        ).fetchall()
        for raw_row in rows:
            row = tuple(raw_row)
            row_id = int(row[0])
            event = row_to_event(row)
            yield row_id, event

    def intent_channel(self, turn_id: str) -> str | None:
        """The channel the turn was submitted from, or ``None`` when it has no intent row."""
        return turn_intent_channel(self._require_conn(), turn_id)

    def speaks(self, turn_id: str, speaks_turn: Callable[[sqlite3.Connection, str], bool]) -> bool:
        """Whether ``speaks_turn`` (ADR 0209) says this actor speaks the turn."""
        return speaks_turn(self._require_conn(), turn_id)

    def playback_started(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> Event:
        """Append the row that opens a playback; the row it answers is the activation."""
        return emit_event(
            self._require_conn(),
            type="surface.playback_started",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def segment_prepared(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> None:
        """Append the row for a segment about to be synthesized."""
        emit_event(
            self._require_conn(),
            type="surface.playback_segment_prepared",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def playback_alignment(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> None:
        """Append the exact text-to-sample mapping of a finished segment."""
        emit_event(
            self._require_conn(), type="surface.playback_alignment",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def checkpoint_recorded(
        self,
        *,
        response_id: str,
        playback_generation_id: int,
        session_id: str,
        heard_text_hash: str | None,
    ) -> bool:
        """Whether a checkpoint of this heard text already stands for the generation."""
        conn = self._require_conn()
        existing = conn.execute(
            "SELECT 1 FROM events WHERE type = 'surface.playback_checkpoint' "
            "AND json_extract(payload_json, '$.response_id') = ? "
            "AND json_extract(payload_json, '$.playback_generation_id') = ? "
            "AND json_extract(payload_json, '$.session_id') = ? "
            "AND json_extract(payload_json, '$.heard_text_hash') = ? LIMIT 1",
            (
                response_id,
                playback_generation_id,
                session_id,
                heard_text_hash,
            ),
        ).fetchone()
        return existing is not None

    def playback_checkpoint(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> None:
        """Append the row that records how far Allen has heard."""
        emit_event(
            self._require_conn(),
            type="surface.playback_checkpoint",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def commit_terminal(  # noqa: PLR0913 - exact terminal shape is explicit
        self,
        active: ActiveResponse,
        *,
        event_type: str,
        snapshot: OutputTimelineSnapshot,
        reason: str | None,
        speech_text_hash: str | None,
        retryable: bool | None,
        starvation_gaps: int,
        host_underflows: int,
        tail_ramp_samples: int,
    ) -> None:
        """Commit the one terminal of ``active``'s playback generation, atomically.

        ``starvation_gaps`` and ``host_underflows`` are the player's counts since the
        generation started, ``tail_ramp_samples`` the player's ramp at the cut.
        """
        payload: dict[str, object] = {
            "session_id": active.lease.session_id,
            "response_id": active.response.response_id,
            "turn_id": active.response.turn_id,
            "playback_generation_id": active.lease.playback_generation_id,
            "heard_through_sequence": snapshot.heard_through_sequence,
            "submitted_samples": snapshot.submitted_samples,
            "estimated_audible_samples": snapshot.estimated_audible_samples,
            "heard_partial_sequence": snapshot.heard_partial_sequence,
            "heard_partial_text_end": snapshot.heard_partial_text_end,
            "total_samples": snapshot.accepted_samples,
            "provider": active.provider_label,
            "cursor_quality": snapshot.cursor_quality,
            "heard_text_hash": snapshot.heard_text_hash,
            "heard_text": snapshot.heard_text,
            "starvation_gaps": starvation_gaps,
            "host_underflows": host_underflows,
            "tail_ramp_samples": tail_ramp_samples,
        }
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = speech_text_hash or hashlib.sha256(b"").hexdigest()
        else:
            payload["reason"] = reason or "unknown"
            if event_type == "surface.playback_failed" and retryable is not None:
                payload["retryable"] = retryable
        terminalize_playback(
            self._require_conn(),
            event_type=event_type,
            payload=payload,
            source_event_id=active.activation_event_uid or active.response.source_event_id,
            correlation={"turn_id": active.response.turn_id},
        )

    def speech_dropped(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> None:
        """Append the row that none of a response was spoken."""
        emit_event(
            self._require_conn(),
            type="surface.speech_dropped",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def lane_isolated(
        self,
        *,
        payload: Mapping[str, object],
        source_event_id: str | None,
        correlation: Mapping[str, str],
    ) -> None:
        """Append the row that this lane will speak nothing again."""
        emit_event(
            self._require_conn(),
            type="surface.playback_lane_isolated",
            payload=payload,
            source_event_id=source_event_id,
            correlation=correlation,
        )

    def tts_usage(
        self,
        usage: Mapping[str, object] | None,
        *,
        response_id: str,
        sequence: int,
    ) -> None:
        """ADR-0018: persist the provider's per-segment character count.

        MiniMax has no balance API, so the dashboard's MiniMax figure is an
        estimate folded from these rows. A segment without ``usage`` (or a
        log write that fails) is skipped: playback must never depend on
        bookkeeping.
        """
        raw = None if usage is None else usage.get("usage_characters")
        if not isinstance(raw, int | float) or isinstance(raw, bool) or raw <= 0:
            return
        try:
            emit_event(
                self._require_conn(),
                type="tts.usage_observed",
                payload={
                    "provider": "minimax",
                    "characters": int(raw),
                    "response_id": response_id,
                    "sequence": sequence,
                    "actor": "observer",
                },
            )
        except Exception:  # bookkeeping never fails playback.
            LOGGER.exception("tts usage: emit failed for %s#%d", response_id, sequence)
