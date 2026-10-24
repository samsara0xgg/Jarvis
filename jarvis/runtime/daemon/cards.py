"""The cards the daemon puts in front of Allen: the confirmation and the question to fill in."""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.transit_tool import live_card
from jarvis.runtime.turn.start import new_turn_id
from jarvis.shared import lang
from jarvis.state.event_log import (
    confirmation_device,
    emit_event,
    iter_events_of_types,
    open_runtime_event_log,
    turn_origin,
)
from jarvis.state.memory_db import remember_fact
from jarvis.state.projections import (
    CLARIFICATION_EVENT_TYPES,
    PendingClarification,
    PendingConfirmations,
)
from jarvis.state.turn_facts import clarification_asked_at, heard_as_barge_pause

if TYPE_CHECKING:
    from jarvis.runtime import JarvisRuntime
    from jarvis.state.projections import PendingConfirmationSlot


# ADR 0144: how long an ask card from a barge-pause fragment stays off the screen, so a
# continuation that folds the fragment in (ADR 0074) arrives before it ever shows.
FRAGMENT_CARD_HOLD_MS: Final[int] = 1_500


def confirmation_card(slot: PendingConfirmationSlot, device: str | None) -> dict[str, Any]:
    """The waiting confirmation as the phone and the Mac read it (ADR 0062, 0321).

    A card of several calls keeps this shape: its question text is the calls' lines one to a
    line, and it has no single tool or arguments to show. A letter the mail tool completed shows
    every recipient by name and address, his own marked (ADR 0366), in the keys the clients read.
    """
    first = slot.calls[0]
    tool = str(first.get("tool_name", ""))
    args_raw = first.get("args_meta")
    args = dict(args_raw) if isinstance(args_raw, Mapping) else {}
    parts = tool.split("__")
    several = len(slot.calls) > 1
    return {
        "id": slot.confirmation_id,
        "tool": tool,
        "action": slot.template_line if several else lang.action(tool)[0],
        "source": parts[1] if len(parts) == 3 and parts[0] == "mcp" else "",  # noqa: PLR2004
        "letter": not several and lang.letter_to(args) is not None,
        "args": {} if several else lang.letter_card(args),
        "device": device,
    }


_CARD_EVENT_TYPES: Final[tuple[str, ...]] = (
    "confirmation.requested", "confirmation.accepted", "confirmation.rejected", "gate.evaluated",
)
"""What the pending card folds from (ADR 0062), the rows the answer path itself re-reads."""


def visible_ask_card(conn: sqlite3.Connection, now_ms: int) -> PendingClarification | None:
    """ADR 0066: the ask card to show, or ``None``.

    ADR 0144: a card its turn put up from a barge-pause fragment stays hidden for
    ``FRAGMENT_CARD_HOLD_MS``. ADR 0206: a continuation no longer closes it, so the hold
    only delays showing it.
    """
    slot = PendingClarification.from_events(iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES))
    if slot is None or not slot.waiting:
        return None
    asked_at = clarification_asked_at(conn, slot.clarification_id)
    if (
        asked_at is not None
        and now_ms - asked_at < FRAGMENT_CARD_HOLD_MS
        and heard_as_barge_pause(conn, slot.asked_turn_id)
    ):
        return None
    return slot


def read_card(runtime: JarvisRuntime) -> dict[str, Any]:
    """ADR 0062: the card waiting for Allen's button, or ``None``.

    ``device`` (ADR 0218) is the one whose turn asked it, ``None`` when no turn did.
    """
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        slot = PendingConfirmations.from_events(
            iter_events_of_types(conn, _CARD_EVENT_TYPES),
        ).slot
        device = None if slot is None else confirmation_device(conn, slot.confirmation_id)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    if slot is None or not slot.is_live(int(time.time() * 1000)):
        return {"card": None}
    return {"card": confirmation_card(slot, device)}


def decide_card(
    runtime: JarvisRuntime, confirmation_id: str, decision: str, edits: dict[str, str], device: str,
) -> str:
    """ADR 0062: a card's button, as an intent the turn pump runs like any other.

    The turn is written under ``device``, the one that pressed it (ADR 0218).
    """
    turn_id = new_turn_id()
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": "",
                "turn_id": turn_id,
                "channel": "card",
                "confirmation_decision": {
                    "confirmation_id": confirmation_id,
                    "decision": decision,
                    "edits": edits,
                },
            },
            correlation={"turn_id": turn_id},
            ingestion_node=device,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return turn_id


def read_question(runtime: JarvisRuntime) -> dict[str, Any]:
    """ADR 0066: the ask card waiting to be filled in, or ``None``.

    ``device`` (ADR 0218) is the one whose turn asked it, ``None`` when unknown.
    """
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        slot = visible_ask_card(conn, int(time.time() * 1000))
        device = None if slot is None else turn_origin(conn, slot.asked_turn_id)[1]
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    if slot is None:
        return {"card": None}
    fields = [
        {k: f[k] for k in ("label", "choices", "value") if k in f} for f in slot.fields
    ]
    card: dict[str, Any] = {
        "id": slot.clarification_id, "question": slot.question, "fields": fields,
        "device": device,
    }
    if slot.trip is not None:  # ADR 0205: a bus card shows only the rows still catchable
        shown = live_card(slot.trip, runtime.clock.now())
        if shown is None:
            return {"card": None}
        card["trip"] = shown
    return {"card": card}


def answer_question(
    runtime: JarvisRuntime, clarification_id: str, answers: dict[str, str] | None, device: str,
) -> str | None:
    """ADR 0066: filled-in answers are remembered and run as Allen's words.

    Dismissing the card (``answers is None``) closes it and runs nothing. The answer's
    turn is written under ``device``, the one that filled it in (ADR 0218).

    Returns the answer turn's id, or ``None`` for a dismissal. Raises
    ``LookupError`` once the card is not the one waiting.
    """
    window_memory = runtime.memory
    conn = open_runtime_event_log(runtime.runtime_paths.event_log)
    try:
        slot = PendingClarification.from_events(
            iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES),
        )
        if slot is None or not slot.waiting or slot.clarification_id != clarification_id:
            msg = "that card is no longer waiting"
            raise LookupError(msg)
        if answers is None:
            emit_event(
                conn,
                type="surface.dismissed",
                payload={
                    "turn_id": slot.asked_turn_id,
                    "clarification_id": clarification_id,
                },
            )
            return None
        filled = [
            (f, answers.get(str(f["label"]), "").strip()) for f in slot.fields
        ]
        filled = [(f, value) for f, value in filled if value]
        if window_memory is not None:
            for f, value in filled:
                if f.get("remember", True):
                    remember_fact(window_memory.db_path, str(f["label"]), value)
        turn_id = new_turn_id()
        emit_event(
            conn,
            type="surface.clarified",
            payload={
                "turn_id": turn_id,
                "clarification_id": clarification_id,
                "answers": {str(f["label"]): value for f, value in filled},
            },
            correlation={"turn_id": turn_id},
            ingestion_node=device,
        )
        emit_event(
            conn,
            type="surface.user_intent",
            payload={
                "transcript": "\n".join(f"{f['label']}: {value}" for f, value in filled),
                "turn_id": turn_id,
                "channel": "clarify",
            },
            correlation={"turn_id": turn_id},
            ingestion_node=device,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return turn_id
