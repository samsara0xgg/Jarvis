"""The Live delegation backend: ``LiveBackend`` and the readers of a turn's outcome."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Final

from jarvis.state.event_log import (
    emit_event,
    iter_events_for_turn,
    iter_events_of_types,
    open_runtime_event_log,
)
from jarvis.state.input_submission_inbox import SubmissionKey, submit_text_once
from jarvis.state.memory_db import MemorySettings, SessionSettings, append_record, brief_note
from jarvis.state.turn_outcome import Answered, Failed, turn_outcome
from jarvis.surface import voice_live
from jarvis.surface.inherent_server.ask import AskOutcome

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event

LOGGER = logging.getLogger(__name__)


LIVE_PRINCIPAL: Final[str] = "gpt_live"
"""ADR-0016 D2: the D21 principal and ``channel`` of a Live delegation."""

LIVE_TERMINAL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "surface.response_emitted",
        "response.completed",
        "response.failed",
        "response.cancelled",
    },
)
"""Committed events that wake a delegation: the answer row and the run terminals.

``response.completed`` commits before the renderer writes ``surface.response_emitted``,
the only row ``lookup_result`` takes as an answer, so a wake on the terminal alone
left the bridge to its 5 s safety poll (measured 5.06 s and 5.9 s, 2026-09-12).
"""

# ADR 0026: a new session is told at most this many finished delegations no
# session was told, none older than this; the rest stay in memory.db and the UI.
_UNDELIVERED_WINDOW_MS: Final[int] = 24 * 60 * 60 * 1000
_UNDELIVERED_MAX: Final[int] = 3


class LiveBackend:
    """ADR-0016 D9 and ADR 0026: the blocking callables the composition root hands to LiveVoice.

    Every method opens its own SQLite connection because LiveVoice calls them
    through ``asyncio.to_thread``, never on the loop thread.
    """

    def __init__(
        self, *, event_log_path: Path, memory: MemorySettings | None, session: SessionSettings,
    ) -> None:
        """Bind the event log delegations are submitted to and the memory they are recorded in."""
        self._event_log_path = event_log_path
        self._memory = memory
        self._session = session

    def delegate(self, text: str, delegation_id: str, session_id: str, record_id: str) -> str:
        """Submit one delegation through the D21 inbox; a replay returns the same turn."""
        key = SubmissionKey(LIVE_PRINCIPAL, session_id, delegation_id)
        conn = open_runtime_event_log(self._event_log_path)
        try:
            receipt = submit_text_once(
                conn,
                key=key,
                transcript=text,
                channel=LIVE_PRINCIPAL,
                source_surface=LIVE_PRINCIPAL,
                record_id=record_id,
            )
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        LOGGER.info(
            "gpt_live delegate %s -> turn %s (replayed=%s)",
            delegation_id, receipt.turn_id, receipt.replayed,
        )
        return receipt.turn_id

    def record(self, source: str, text: str, record_id: str) -> None:
        """One memory.db row; a repeated id is a no-op (D3)."""
        if self._memory is None:
            return
        append_record(self._memory.db_path, record_id=record_id, source=source, text=text)

    def brief(self) -> str:
        """The budgeted startup history for ``session.start.input`` (D7)."""
        if self._memory is None:
            return ""
        return brief_note(self._memory.db_path, max_chars=self._session.live_brief_max_chars)

    def lookup_result(self, turn_id: str) -> voice_live.DelegationResult | None:
        """The turn's final answer, its failure, or None while it is still running (D4, D5)."""
        conn = open_runtime_event_log(self._event_log_path)
        try:
            return _delegation_result(conn, turn_id)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def undelivered_results(self) -> list[voice_live.UndeliveredResult]:
        """Finished delegations of the last day that no session was told, oldest first."""
        since = int(time.time() * 1000) - _UNDELIVERED_WINDOW_MS
        conn = open_runtime_event_log(self._event_log_path)
        found: list[voice_live.UndeliveredResult] = []
        try:
            intents = [
                event
                for event in iter_events_of_types(
                    conn, ("surface.user_intent",), since_epoch_ms=since,
                )
                if event.payload.get("channel") == LIVE_PRINCIPAL
            ]
            for intent in reversed(intents):
                turn_id = str(intent.payload.get("turn_id", ""))
                if not turn_id or any(
                    iter_events_for_turn(conn, turn_id, ("live.result_delivered",)),
                ):
                    continue
                outcome = _delegation_result(conn, turn_id)
                if outcome is None:
                    continue  # still running: no outcome yet; the next start looks again
                found.append(
                    voice_live.UndeliveredResult(
                        turn_id=turn_id,
                        request=str(intent.payload.get("transcript", "")),
                        result=outcome,
                    ),
                )
                if len(found) == _UNDELIVERED_MAX:
                    break
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        found.reverse()
        return found

    def mark_delivered(self, turn_id: str, session_id: str, kind: str) -> None:
        """Persist that ``session_id`` was told ``turn_id``'s outcome, or that it was withheld."""
        self._emit(
            "live.result_delivered",
            {"turn_id": turn_id, "session_id": session_id, "kind": kind},
            correlation={"turn_id": turn_id},
        )

    def record_usage(
        self,
        session_id: str,
        seconds: float | None,
        reason: str,
        server_reason: str | None,
        final: bool,  # noqa: FBT001 - Callable shape fixed by LiveVoice
    ) -> None:
        """One ``live.session_usage`` row per closed session (ADR 0026)."""
        self._emit(
            "live.session_usage",
            {
                "session_id": session_id,
                "seconds": seconds,
                "final": final,
                "reason": reason,
                "server_reason": server_reason,
            },
        )

    def _emit(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        correlation: dict[str, str] | None = None,
    ) -> None:
        conn = open_runtime_event_log(self._event_log_path)
        try:
            emit_event(conn, type=event_type, payload=payload, correlation=correlation)
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _delegation_result(
    conn: sqlite3.Connection, turn_id: str,
) -> voice_live.DelegationResult | None:
    """The turn's final answer, its failure, or None while it is still running (ADR 0349)."""
    outcome = turn_outcome(conn, turn_id)
    if isinstance(outcome, Answered):
        return voice_live.DelegationResult(
            status="answered", text=outcome.text, voice_text=outcome.voice_text,
        )
    if isinstance(outcome, Failed):
        return voice_live.DelegationResult(
            status="failed", reason=outcome.reason or outcome.detail or outcome.event_type,
        )
    return None


def ask_outcome(event_log_path: Path, turn_id: str) -> AskOutcome | None:
    """How the turn behind ``POST /inherent/ask`` ended, or None while it is still running.

    The answer is the turn's final one: ``spoken`` its voice text (the document text, else the
    text, of an answer with no channels) and ``written`` its document text when that is what
    the voice leaves out (``written_apart``, ADR 0114), as the ``done`` envelope carries them.
    """
    conn = open_runtime_event_log(event_log_path)
    try:
        outcome = turn_outcome(conn, turn_id)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    if isinstance(outcome, Answered):
        return AskOutcome(
            spoken=outcome.voice_text or outcome.document_text or outcome.text,
            written=(outcome.document_text or "") if outcome.written_apart else "",
        )
    if isinstance(outcome, Failed):
        return AskOutcome(failure=outcome.reason or outcome.event_type)
    return None


def live_wake_subscriber(live_voice: voice_live.LiveVoice) -> Callable[[Event], None]:
    """Bus subscriber: a response terminal wakes the delegation that owns its turn (D9).

    Runs on the committing thread; ``deliver`` only schedules onto the loop.
    """

    def _wake(event: Event) -> None:
        if event.type in LIVE_TERMINAL_TYPES:
            turn_id = event.payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                live_voice.deliver(turn_id)

    return _wake
