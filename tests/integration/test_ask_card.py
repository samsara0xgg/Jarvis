"""ADR 0066 and 0206 acceptance: the ask card's lifecycle in the event log, and the facts it keeps.

Each case writes the events the tool, the companion's routes and Allen's words
write, then reads what the companion and the model would see: whether the card
is still waiting, and the one status line the model gets on the turn that ends it.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from jarvis.decision.packet import assemble_packet, format_pending_clarification_note
from jarvis.execution import tools
from jarvis.execution.tools import ToolError, build_default_registry
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


_STILL_UP = (
    f"When this turn began, your card {_ASKED} was still on screen. "
    "If the user's words answer it, carry on with them "
    "and take it down with close_question in the same step; if it no longer applies, take it "
    "down; otherwise leave it."
)


@pytest.mark.parametrize("spoken", [1, 2, 3])
def test_speaking_over_the_card_leaves_it_up_and_every_turn_hears_it_is_still_on_screen(
    conn: sqlite3.Connection, spoken: int,
) -> None:
    """ADR 0206: his utterances do not close the card; each of those turns carries the note."""
    _ask(conn, "Q1")
    for n in range(spoken):
        said = _says(conn, f"T{n}", "1 Test St, V8W 1A1")
        assert _card(conn).waiting
        assert _note(conn, said) == _STILL_UP
    shown = _visible_ask_card(conn, _asked_at(conn) + _FRAGMENT_CARD_HOLD_MS)
    assert shown is not None
    assert shown.clarification_id == "Q1"


def test_a_card_he_spoke_over_still_takes_his_submit(conn: sqlite3.Connection) -> None:
    """ADR 0206: the submit path asks only `waiting`; after two utterances it still closes by id."""
    _ask(conn, "Q1")
    _says(conn, "T1")
    _says(conn, "T2")
    assert _card(conn).waiting
    emit_event(conn, type="surface.clarified", payload={
        "turn_id": "T-answer", "clarification_id": "Q1", "answers": {"送餐地址": "1 Test St"},
    })
    closed = _card(conn)
    assert (closed.waiting, closed.answered_turn_id) == (False, "T-answer")


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


def test_a_card_he_spoke_over_and_then_closed_tells_the_next_turn_it_went_unanswered(
    conn: sqlite3.Connection,
) -> None:
    """ADR 0206: his close button after speaking over it still reads as his close."""
    _ask(conn, "Q1")
    _says(conn, "T1")
    emit_event(
        conn, type="surface.dismissed", payload={"turn_id": "T-ask", "clarification_id": "Q1"},
    )
    assert not _card(conn).waiting
    assert _note(conn, _says(conn, "T2")) == (
        f"The user closed your card {_ASKED} without filling it in."
    )


def _take_down(conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Run `close_question` as the dispatcher would, for action A1."""
    monkeypatch.setattr(tools, "_get_running_event_uid", lambda *_: None)
    (definition,) = (
        d for d in build_default_registry().get_definitions() if d.name == "close_question"
    )
    ctx = SimpleNamespace(conn=conn, action_id="A1")
    return dict(definition.handler({}, ctx))  # type: ignore[arg-type,call-arg,misc]


def test_close_question_takes_the_card_down_and_the_next_turn_hears_no_close_note(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0206: her take-down closes the card, and is not his close, so no note says so."""
    _ask(conn, "Q1")
    _says(conn, "T1")
    assert _take_down(conn, monkeypatch) == {"status": "closed", "question": _QUESTION}
    closed = _card(conn)
    assert (closed.waiting, closed.dismissed, closed.just_dismissed) == (False, False, False)
    assert _visible_ask_card(conn, _asked_at(conn) + _FRAGMENT_CARD_HOLD_MS) is None
    assert _note(conn, _says(conn, "T2")) is None


def test_close_question_errors_when_no_question_card_waits(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing asked, taken down, closed by him, or a bus card: each is code no_card."""
    def refused() -> str:
        with pytest.raises(ToolError) as caught:
            _take_down(conn, monkeypatch)
        return caught.value.code

    assert refused() == "no_card"
    _ask(conn, "Q1")
    _take_down(conn, monkeypatch)
    assert refused() == "no_card"
    _ask(conn, "Q2")
    emit_event(
        conn, type="surface.dismissed", payload={"turn_id": "T-ask", "clarification_id": "Q2"},
    )
    assert refused() == "no_card"
    emit_event(conn, type="clarification.requested", payload={
        "clarification_id": "Q3", "question": "mall", "fields": [], "turn_id": "T-ask",
        "trip": {"offer_id": "Q3", "to": "mall", "options": [], "modes": {}},
    })
    assert refused() == "no_card"


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
    today = datetime.now().astimezone().date().isoformat()  # ADR 0201: each line carries a note
    assert render_context(db, exclude_id="").profile == (
        f"[About the user]\n### 关于你\n- ({today}, set) 送餐地址: 2 Sample Rd"
        f"\n- ({today}, set) 饮食偏好: 不吃香菜"
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
    """ADR 0144: the fragment's card is hidden for the hold, then shown if nothing folded it."""
    _heard(conn, "T-ask", "barge_pause")
    _ask(conn, "Q1")
    at = _asked_at(conn)
    assert _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS - 1) is None
    shown = _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS)
    assert shown is not None
    assert shown.clarification_id == "Q1"


def test_a_continuation_inside_the_hold_no_longer_closes_the_fragment_card(
    conn: sqlite3.Connection,
) -> None:
    """ADR 0206 over ADR 0144: the rest of his sentence leaves the card; the hold still hides it."""
    _heard(conn, "T-ask", "barge_pause")
    _ask(conn, "Q1")
    at = _asked_at(conn)
    _heard(conn, "T-rest", "acoustic_pause")
    assert _visible_ask_card(conn, at + 10) is None
    shown = _visible_ask_card(conn, at + _FRAGMENT_CARD_HOLD_MS + 1)
    assert shown is not None
    assert shown.clarification_id == "Q1"


def test_a_card_from_any_other_turn_shows_at_once(conn: sqlite3.Connection) -> None:
    """ADR 0144: only a barge-pause fragment's card is held."""
    _heard(conn, "T-ask", "acoustic_pause")
    _ask(conn, "Q1")
    shown = _visible_ask_card(conn, _asked_at(conn))
    assert shown is not None
    assert shown.clarification_id == "Q1"
