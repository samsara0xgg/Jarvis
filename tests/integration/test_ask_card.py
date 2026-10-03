"""ADR 0066 acceptance: the ask card's lifecycle in the event log, and the facts it keeps.

Each case writes the events the tool, the companion's routes and Allen's words
write, then reads what the companion and the model would see: whether the card
is still waiting, and the one status line the model gets on the turn that ends it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from jarvis.decision.packet import assemble_packet, format_pending_clarification_note
from jarvis.runtime.inherent_loop import _FRAGMENT_CARD_HOLD_MS, _visible_ask_card
from jarvis.state.event_log import emit_event, iter_events_of_types, open_event_log
from jarvis.state.memory_db import remember_fact, render_context
from jarvis.state.projections import CLARIFICATION_EVENT_TYPES, PendingClarification

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event

_QUESTION = "Where to?"
_FIELDS = [{"label": "送餐地址", "remember": True}, {"label": "邮编", "remember": True}]
_ASKED = f"“{_QUESTION}” (送餐地址, 邮编)"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    """A fresh event log."""
    return open_event_log(tmp_path / "events.db")


def _ask(conn: sqlite3.Connection, clarification_id: str, question: str = _QUESTION) -> None:
    emit_event(conn, type="clarification.requested", payload={
        "clarification_id": clarification_id, "question": question, "fields": _FIELDS,
        "turn_id": "T-ask",
    })


def _says(conn: sqlite3.Connection, turn_id: str, text: str = "hi") -> Event:
    return emit_event(
        conn, type="utterance.received", payload={"transcript": text, "turn_id": turn_id},
        correlation={"turn_id": turn_id},
    )


def _card(conn: sqlite3.Connection) -> PendingClarification:
    card = PendingClarification.from_events(
        iter_events_of_types(conn, CLARIFICATION_EVENT_TYPES),
    )
    assert card is not None
    return card


def _note(conn: sqlite3.Connection, trigger: Event) -> str | None:
    return format_pending_clarification_note(assemble_packet(trigger, conn))


def test_a_filled_in_card_closes_and_its_answer_turn_hears_what_it_answers(
    conn: sqlite3.Connection,
) -> None:
    """GET shows the card until surface.clarified; the answer turn gets the answer line.

    邮编 came back blank, so that line also says not to ask for it again.
    """
    _ask(conn, "Q1")
    assert _card(conn).waiting
    assert [f["label"] for f in _card(conn).fields] == ["送餐地址", "邮编"]
    emit_event(conn, type="surface.clarified", payload={
        "turn_id": "T-answer", "clarification_id": "Q1", "answers": {"送餐地址": "1 Test St"},
    })
    answer = emit_event(conn, type="surface.user_intent", payload={
        "transcript": "送餐地址: 1 Test St", "turn_id": "T-answer", "channel": "clarify",
    }, correlation={"turn_id": "T-answer"})
    assert not _card(conn).waiting
    assert _note(conn, answer) == (
        f"This message is the user's answer to your card {_ASKED}. "
        "They left 邮编 blank: decide those yourself or do without them; "
        "do not ask for them again. "
        "Fields you did not mark one-off are already saved in [About the user]. "
        "Carry on with what they asked for."
    )
    assert _note(conn, _says(conn, "T-later")) is None


def test_speaking_over_the_card_closes_it_and_only_that_turn_hears_the_question(
    conn: sqlite3.Connection,
) -> None:
    """The first utterance after the ask closes the card and carries its question."""
    _ask(conn, "Q1")
    first = _says(conn, "T1", "1 Test St, V8W 1A1")
    assert not _card(conn).waiting
    assert _note(conn, first) == (
        f"Your card {_ASKED} closed because the user spoke instead "
        "of filling it in. If these words answer it, carry on with them."
    )
    assert _note(conn, _says(conn, "T2")) is None


def test_a_dismissed_card_runs_nothing_and_the_next_turn_hears_it_went_unanswered(
    conn: sqlite3.Connection,
) -> None:
    """surface.dismissed closes the card; the next utterance, and only it, is told."""
    _ask(conn, "Q1")
    emit_event(
        conn, type="surface.dismissed", payload={"turn_id": "T-ask", "clarification_id": "Q1"},
    )
    assert not _card(conn).waiting
    assert _note(conn, _says(conn, "T1")) == (
        f"The user closed your card {_ASKED} without filling it in."
    )
    assert _note(conn, _says(conn, "T2")) is None


def test_a_newer_ask_replaces_the_card_and_a_stale_close_is_ignored(
    conn: sqlite3.Connection,
) -> None:
    """Only the newest card waits; closing the older id leaves it up."""
    _ask(conn, "Q1")
    _ask(conn, "Q2", "How many people?")
    emit_event(
        conn, type="surface.dismissed", payload={"turn_id": "T-ask", "clarification_id": "Q1"},
    )
    card = _card(conn)
    assert card.clarification_id == "Q2"
    assert card.question == "How many people?"
    assert card.waiting


def test_a_kept_fact_is_one_about_the_user_line_and_its_topic_rewrites_it_in_place(
    tmp_path: Path,
) -> None:
    """remember_fact lands in [About the user]; the same topic replaces the row where it was."""
    db = tmp_path / "memory.db"
    remember_fact(db, "送餐地址", "1 Test St")
    remember_fact(db, "饮食偏好", "不吃香菜")
    remember_fact(db, "送餐地址", "2 Sample Rd")
    assert render_context(db, exclude_id="").profile == (
        "[About the user]\n- 送餐地址: 2 Sample Rd\n- 饮食偏好: 不吃香菜"
    )


def _heard(conn: sqlite3.Connection, turn_id: str, reason: str) -> None:
    emit_event(
        conn, type="utterance.received",
        payload={"transcript": "hi", "turn_id": turn_id, "endpoint_reason": reason},
        correlation={"turn_id": turn_id},
    )


def _asked_at(conn: sqlite3.Connection) -> int:
    ask = list(iter_events_of_types(conn, ("clarification.requested",)))[-1]
    return ask.ts_epoch_ms


def test_a_card_from_a_barge_pause_fragment_waits_out_the_hold_before_it_shows(
    conn: sqlite3.Connection,
) -> None:
    """ADR 0142: the fragment's card is hidden for the hold, then shown if nothing folded it."""
    _heard(conn, "T-ask", "barge_pause")
    _ask(conn, "Q1")
    at = _asked_at(conn)
    assert _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS - 1) is None
    shown = _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS)
    assert shown is not None
    assert shown.clarification_id == "Q1"


def test_a_continuation_inside_the_hold_closes_the_fragment_card_unseen(
    conn: sqlite3.Connection,
) -> None:
    """ADR 0142: the rest of his sentence folds the fragment in before the hold ends."""
    _heard(conn, "T-ask", "barge_pause")
    _ask(conn, "Q1")
    at = _asked_at(conn)
    _heard(conn, "T-rest", "acoustic_pause")
    assert _visible_ask_card(conn, at + 10) is None
    assert _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS + 1) is None


def test_a_card_from_any_other_turn_shows_at_once(conn: sqlite3.Connection) -> None:
    """ADR 0142: only a barge-pause fragment's card is held."""
    _heard(conn, "T-ask", "acoustic_pause")
    _ask(conn, "Q1")
    shown = _visible_ask_card(conn, _asked_at(conn))
    assert shown is not None
    assert shown.clarification_id == "Q1"
