"""ADR 0251: the runtime's deep lane, the owner of the turns the voice lane hands over.

A deep task is a ``surface.user_intent`` on :data:`DEEP_CHANNEL`. The intent pump claims it and
``drive_turn`` runs it like any turn (the settings and the rules it reads are
``jarvis.decision.deep``). This module writes the opening row, refuses a second task while one
runs, and writes the notice a failed one leaves.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from jarvis.decision.deep import LOST_AFTER_S, TASK_MAX_CHARS, DeepSettings
from jarvis.decision.llm import failure_reason
from jarvis.shared import lang
from jarvis.state.event_log import DEEP_CHANNEL, MAC_NODE, emit_event, turn_origin
from jarvis.state.turn_overlap import open_deep_turn

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event
    from jarvis.state.committed_event_bus import CommittedEventBus

LOGGER = logging.getLogger(__name__)


class DeepLane:
    """``llm.deep`` made live: it starts the deep turns and says why one could not run."""

    def __init__(
        self,
        settings: DeepSettings,
        *,
        reply_language: str = "follow",
        committed_event_bus: CommittedEventBus | None = None,
    ) -> None:
        """Hold the settings; the lock keeps two hand-overs from both finding the lane free."""
        self.settings = settings
        self._reply_language = reply_language
        self._bus = committed_event_bus
        self._lock = threading.Lock()

    def hand_over(
        self, conn: sqlite3.Connection, *, task: str, from_turn: str | None,
    ) -> dict[str, Any]:
        """Start the deep turn for ``task``, or answer that one is already running."""
        said_in = lang.reply_language(task, self._reply_language)
        with self._lock:
            running = open_deep_turn(conn, since_ms=int((time.time() - LOST_AFTER_S) * 1000))
            if running is not None:
                return {
                    "started": False,
                    "reason": "busy",
                    "spoken": lang.t("deep.busy", lang=said_in),
                }
            turn_id = self._open(
                conn, task=task, from_turn=from_turn, node=turn_origin(conn, from_turn or "")[1],
            )
        return {"started": True, "turn_id": turn_id, "spoken": lang.t("deep.started", lang=said_in)}

    def failed(self, conn: sqlite3.Connection, intent: Event, exc: BaseException) -> None:
        """Leave a notice that the task ``intent`` opened did not finish; never raises.

        The notice is a deep turn of its own that answers in one fixed sentence, so it reaches
        him the way a result does: spoken when he is still talking with her, else a card.
        Nothing is owed for another turn, for the notice itself, or for a task whose answer
        already went out (a raise after it would say "没查成" beside the result).
        """
        if intent.payload.get("channel") != DEEP_CHANNEL or intent.payload.get("deep_failure"):
            return
        try:
            if conn.execute(
                "SELECT 1 FROM events WHERE type = 'surface.response_emitted' "
                "AND json_extract(payload_json, '$.turn_id') = ? LIMIT 1",
                (intent.payload.get("turn_id"),),
            ).fetchone():
                return
            self._open(
                conn,
                task=str(intent.payload.get("transcript", "")),
                from_turn=str(intent.payload.get("turn_id", "")),
                node=turn_origin(conn, str(intent.payload.get("turn_id", "")))[1],
                failure=failure_reason(exc),
            )
        except Exception:  # the failure is already on the log; this notice is a courtesy
            LOGGER.exception("deep lane: the failure notice could not be written")

    def _open(
        self,
        conn: sqlite3.Connection,
        *,
        task: str,
        from_turn: str | None,
        node: str | None,
        failure: str | None = None,
    ) -> str:
        turn_id = f"T{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "transcript": task[:TASK_MAX_CHARS],
            "turn_id": turn_id,
            "channel": DEEP_CHANNEL,
            "from_turn": from_turn,
        }
        if failure is not None:
            payload["deep_failure"] = failure
        emit_event(
            conn,
            type="surface.user_intent",
            payload=payload,
            correlation={"turn_id": turn_id},
            ingestion_node=node or MAC_NODE,
            committed_event_bus=self._bus,
        )
        return turn_id


__all__ = ["DeepLane"]
