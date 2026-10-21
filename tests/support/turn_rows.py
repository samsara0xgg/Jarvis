"""A real Event Log and the rows one turn leaves in it, for tests of what L2 says about a turn."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event


class TurnLog:
    """An on-disk Event Log in ``root``; each method writes the rows of one thing a turn does.

    Every writer takes the turn id and an optional ``at`` (epoch ms), so a test that reads a time
    window never depends on the wall clock.
    """

    def __init__(self, root: Path) -> None:
        """Open a fresh log under ``root``."""
        self.conn: sqlite3.Connection = open_event_log(root / "events.db")

    def put(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        at: int | None = None,
        source: Event | None = None,
    ) -> Event:
        """Append one row, correlated to ``turn_id`` when there is one."""
        return emit_event(
            self.conn,
            type=event_type,
            payload=payload,
            correlation=None if turn_id is None else {"turn_id": turn_id},
            ts_epoch_ms=at,
            source_event_id=None if source is None else source.event_uid,
        )

    def heard(
        self,
        turn_id: str,
        words: str = "hello",
        *,
        at: int | None = None,
        **more: Any,  # noqa: ANN401 - payload fields
    ) -> Event:
        """His voice sentence: an ``utterance.received``."""
        return self.put(
            "utterance.received",
            {"transcript": words, "turn_id": turn_id, **more},
            turn_id=turn_id,
            at=at,
        )

    def typed(self, turn_id: str, channel: str, *, at: int | None = None) -> Event:
        """His words on ``channel``: a ``surface.user_intent``."""
        return self.put(
            "surface.user_intent",
            {"transcript": "hello", "turn_id": turn_id, "channel": channel},
            turn_id=turn_id,
            at=at,
        )

    def claimed(self, turn_id: str, trigger: Event | None) -> Event:
        """The ``turn.started`` that claims the turn from ``trigger``."""
        return self.put("turn.started", {"turn_id": turn_id}, turn_id=turn_id, source=trigger)

    def run(
        self,
        turn_id: str,
        response_id: str,
        phase: str = "final",
        *,
        corrects: str | None = None,
        at: int | None = None,
    ) -> Event:
        """A response run opening: ``response.started``."""
        payload: dict[str, Any] = {
            "response_id": response_id,
            "response_group_id": f"G-{turn_id}",
            "turn_id": turn_id,
            "phase": phase,
            "channel": "both",
            "emission_mode": "full_text",
            "output_risk_class": "low",
            "required_gate_mode": "pre_emit",
            "policy_hash": "h",
            "active_subject_ref": None,
            "evidence_snapshot_hash": "h",
        }
        if corrects is not None:
            payload["corrects_response_id"] = corrects
        return self.put("response.started", payload, turn_id=turn_id, at=at)

    def run_ends(
        self,
        kind: str,
        turn_id: str,
        response_id: str,
        reason: str = "user_stop",
        *,
        at: int | None = None,
    ) -> Event:
        """A run's terminal row: ``kind`` is ``response.completed``, ``.cancelled``, ``.failed``."""
        payload: dict[str, Any] = {
            "response_id": response_id,
            "response_group_id": f"G-{turn_id}",
            "turn_id": turn_id,
        }
        if kind == "response.completed":
            payload["response_hash"] = "h"
        else:
            payload["reason"] = reason
        return self.put(kind, payload, turn_id=turn_id, at=at)

    def emitted(
        self,
        turn_id: str,
        response_id: str,
        text: str,
        phase: str = "final",
        *,
        at: int | None = None,
        **more: Any,  # noqa: ANN401 - payload fields
    ) -> Event:
        """The answer (or wait line) reaching the surfaces: ``surface.response_emitted``."""
        return self.put(
            "surface.response_emitted",
            {"turn_id": turn_id, "text": text, "response_id": response_id, "phase": phase, **more},
            turn_id=turn_id,
            at=at,
        )

    def opened(self, turn_id: str, response_id: str, phase: str = "final") -> Event:
        """The answer's first segment opening on a surface: ``surface.response_open``."""
        return self.put(
            "surface.response_open",
            {"turn_id": turn_id, "query": "q", "kind": "text", "response_id": response_id,
             "phase": phase},
            turn_id=turn_id,
        )

    def turn_failed(
        self, turn_id: str, *, reason: str | None = None, exception: str = "KeyError('k')",
    ) -> Event:
        """The turn raising: ``turn.failed``."""
        payload: dict[str, Any] = {"turn_id": turn_id, "exception_repr": exception}
        if reason is not None:
            payload["reason"] = reason
        return self.put("turn.failed", payload, turn_id=turn_id)

    def turn_ended(self, turn_id: str, *, at: int | None = None) -> Event:
        """The turn finishing: ``turn.ended``."""
        return self.put("turn.ended", {"turn_id": turn_id}, turn_id=turn_id, at=at)
