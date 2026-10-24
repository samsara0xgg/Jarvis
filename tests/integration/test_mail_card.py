"""ADR 0063: the one mail a turn read is the mail record, whichever way the ``mail`` tool read it.

The tool runs through the real dispatcher over the Gmail fake; each call is first proposed in a
turn, as the decision loop records it, and ``fetched_mail`` reads the turn back. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.runtime.turn.finish import fetched_mail
from jarvis.state.action_facts import action_result
from jarvis.state.event_log import emit_event
from tests.support.clock import FakeClock
from tests.support.gmail_fake import SENT, Gmail
from tests.support.tool_world import ToolWorld, tool_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.shared import RawResult

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)  # 11:00 on Wednesday in Vancouver
ME = "Allen <allen@example.com>"
SAM = "Sam Lee <sam@example.com>"
KIM = "Kim Ray <kim@example.com>"
TURN = "T1"


class Turn:
    """One turn of the model's calls to ``mail``, each recorded as proposed before it runs."""

    def __init__(self, tmp_path: Path, gmail: Gmail) -> None:
        """Wire the tool over ``gmail`` with a dispatcher and an event log."""
        (tool,) = build_mail_tool(Mailbox(gmail.tool))
        self.world = ToolWorld(
            tmp_path, tools=(tool,), clock=FakeClock(NOW, zone=ZoneInfo("America/Vancouver")),
        )
        self._n = 0

    def call(self, **args: Any) -> RawResult:  # noqa: ANN401 — the model's arguments.
        """Propose one call in the turn and run it."""
        self._n += 1
        action_id = f"A{self._n}"
        emit_event(
            self.world.conn, type="action.proposed", correlation={"turn_id": TURN},
            payload={
                "turn_id": TURN, "action_id": action_id, "tool_name": "mail", "arguments": args,
                "caller_principal": "jarvis_llm", "risk_level": "L0",
            },
        )
        return self.world.dispatch(tool_request("mail", action_id, arguments=args))

    def card(self) -> tuple[str, str] | None:
        """What the turn leaves as the mail record."""
        return fetched_mail(self.world.conn, TURN)

    def result_uid(self, n: int) -> str:
        """The event uid of the nth call's answer."""
        found = action_result(self.world.conn, f"A{n}")
        assert found is not None
        return found[1].event_uid


def _mailbox() -> Gmail:
    gmail = Gmail(now=NOW)
    gmail.add("sent0", "Hello", sender=ME, to=SAM, labels=SENT, at=NOW - timedelta(days=40))
    gmail.add(
        "hours", "Re: Hours", sender=SAM, to=ME, thread="t-hours", at=NOW - timedelta(hours=3),
        plain="Office hours moved to Thursday at 2.\n\n> Are they still Tuesday?",
    )
    gmail.add(
        "trip", "Trip plan", sender=KIM, to=ME, thread="t-trip", at=NOW - timedelta(hours=2),
        plain="Flights are booked.",
    )
    gmail.add(
        "trip2", "Re: Trip plan", sender=SAM, to=ME, thread="t-trip", at=NOW - timedelta(hours=1),
        plain="Which hotel?",
    )
    paragraphs = [f"Paragraph {n}. " + ("The quick brown fox jumps. " * 15) for n in range(1, 25)]
    gmail.add("long", "The long report", sender=KIM, to=ME, plain="\n\n".join(paragraphs))
    return gmail


@pytest.fixture
def turn(tmp_path: Path) -> Iterator[Turn]:
    """A turn over a small invented mailbox."""
    made = Turn(tmp_path, _mailbox())
    yield made
    made.world.close()


def test_a_mail_read_in_a_turn_is_the_record_with_its_headers_and_the_text_the_tool_gave(
    turn: Turn,
) -> None:
    """Headers a reply needs, a blank line, the mail's new text; the id is the answer's uid."""
    assert turn.call(action="read", message="hours").error is None
    found = turn.card()
    assert found is not None
    assert found[0] == turn.result_uid(1)
    assert found[1] == (
        f"From: {SAM}\nTo: {ME}\nDate: 2026-10-21T08:00-07:00\nSubject: Re: Hours\n"
        "Thread-Id: t-hours\nGmail-Id: hours\n\nOffice hours moved to Thursday at 2."
    )


def test_a_search_with_one_match_is_the_record_as_well(turn: Turn) -> None:
    """The tool reads the one match whole, so the turn read one mail."""
    assert turn.call(action="search", query="hours").error is None
    found = turn.card()
    assert found is not None
    assert "Gmail-Id: hours" in found[1]
    assert found[1].endswith("Office hours moved to Thursday at 2.")


def test_the_parts_of_one_long_mail_are_the_record_in_the_order_they_were_read(
    turn: Turn,
) -> None:
    """Part 2 read before part 1 and part 1 read twice: the record keeps the order, once each."""
    first = turn.call(action="read", message="long").payload["text"]
    second = turn.call(action="read", message="long", page=2).payload["text"]
    turn.call(action="read", message="long")
    found = turn.card()
    assert found is not None
    assert found[0] == turn.result_uid(3)
    assert found[1].partition("\n\n")[2] == f"{first}\n\n{second}"


def test_a_turn_that_read_two_mails_is_a_list_and_has_no_record(turn: Turn) -> None:
    """One after the other, and three at once, are answered in words."""
    assert turn.call(action="read", message="hours").error is None
    assert turn.call(action="read", message="trip").error is None
    assert turn.card() is None


def test_several_mails_in_one_call_have_no_record(turn: Turn) -> None:
    """The three-at-once answer carries three mails."""
    both = turn.call(action="read", message="hours,trip")
    assert both.error is None
    assert [one["message"] for one in both.payload["mails"]] == ["hours", "trip"]
    assert turn.card() is None


def test_a_conversation_is_several_messages_and_has_no_record(turn: Turn) -> None:
    """Reading a conversation reads each of its messages; none of them is the card."""
    answer = turn.call(action="thread", message="trip")
    assert answer.error is None
    assert [one["message"] for one in answer.payload["messages"]] == ["trip", "trip2"]
    assert turn.card() is None


def test_a_mail_read_after_its_conversation_is_still_two_messages_and_has_no_record(
    turn: Turn,
) -> None:
    """The conversation read two messages; one of them read again does not make it one."""
    assert turn.call(action="thread", message="trip").error is None
    assert turn.call(action="read", message="trip").error is None
    assert turn.card() is None


def test_an_attachment_is_not_a_mail_and_adds_nothing_to_the_record(tmp_path: Path) -> None:
    """The file's text is not the mail's; its mail still is the record, and alone it is none."""
    gmail = _mailbox()
    gmail.add(
        "memo", "The memo", sender=KIM, to=ME, at=NOW - timedelta(minutes=30),
        plain="See the attached note.",
        attachments=[("note.txt", "text/plain", b"The note says the flight leaves at nine.\n")],
    )
    run = Turn(tmp_path, gmail)
    try:
        assert run.call(action="attachment", message="memo", file="note.txt").error is None
        assert run.card() is None
        run.call(action="read", message="memo")
        found = run.card()
        assert found is not None
        assert found[1].endswith("See the attached note.")
        assert "flight leaves" not in found[1]
    finally:
        run.world.close()


def test_a_failed_call_and_the_calls_that_read_no_mail_leave_no_record(turn: Turn) -> None:
    """A mail Gmail lacks, a list and an archive: none of them is a mail read."""
    assert turn.call(action="read", message="nothing-here").error is not None
    turn.call()
    turn.call(action="archive", message="hours")
    assert turn.card() is None
    assert turn.call(action="read", message="hours").error is None
    assert turn.card() is not None


def test_a_call_with_no_answer_yet_is_no_record(turn: Turn) -> None:
    """Proposed, not run: there is no message to show."""
    emit_event(
        turn.world.conn, type="action.proposed", correlation={"turn_id": TURN},
        payload={
            "turn_id": TURN, "action_id": "A9", "tool_name": "mail",
            "arguments": {"action": "read", "message": "hours"}, "caller_principal": "jarvis_llm",
            "risk_level": "L0",
        },
    )
    assert turn.card() is None
