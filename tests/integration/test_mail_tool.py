"""ADR 0362: his mail is one tool that lists, searches, reads and archives, and remembers its rows.

The tool runs through the real flat dispatcher and event log over a fake of the Workspace server
(``tests.support.gmail_fake``) that holds invented mail. Each check asserts what the model is
sent, what the tool asked Gmail for, or the receipt and event it left; no network, no key.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.policy import validate_requires_confirmation
from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_lists import Ask, Waiting
from jarvis.execution.mail_tool import (
    ARCHIVE_MAX,
    READ_MAX,
    _waiting_summary,
    build_mail_tool,
)
from jarvis.execution.tools import ReadOnlyToolRegistry, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import call_asks, call_risk, only_reads
from jarvis.state.event_log import emit_event, iter_events_of_types
from tests.support.gmail_fake import PROMOTIONS, SENT, UPDATES, Gmail
from tests.support.mail_raw import pdf
from tests.support.mail_rig import (
    BANK,
    BOT,
    KIM,
    ME,
    NEWS,
    NOW,
    SAM,
    SHOP,
    ZONE,
    Rig,
    ago,
    mailbox,
    receipt_of,
)
from tests.support.tool_world import tool_request

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


ACTIONS = ("list", "search", "read", "thread", "attachment", "draft", "send", "archive")




def _morning() -> Gmail:
    """Fourteen unread: five from people, seven shop promotions, two from his bank."""
    gmail = mailbox()
    for n, minutes in enumerate((10, 20, 30, 40, 50)):
        gmail.add(
            f"p{n}", f"Plan {n}", sender=SAM if n % 2 == 0 else KIM, to=ME,
            at=ago(minutes=minutes), plain=f"Hello, this is plan {n}.\n\nDetails follow.",
        )
    for n, minutes in enumerate((15, 25, 35, 45, 55, 65, 75)):
        gmail.add(
            f"q{n}", f"Deal {n}", sender=SHOP, to=ME, at=ago(minutes=minutes), labels=PROMOTIONS,
            headers=NEWS, plain="Big sale.",
        )
    for n, minutes in enumerate((5, 60)):
        gmail.add(
            f"u{n}", f"Alert {n}", sender=BANK, to=ME, at=ago(minutes=minutes), labels=UPDATES,
            headers=BOT, plain="Statement ready.",
        )
    return gmail


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    """The tool over the Wednesday-morning mailbox."""
    made = Rig(tmp_path, _morning())
    yield made
    made.close()


def _rig(tmp_path: Path, gmail: Gmail, **kwargs: Any) -> Rig:  # noqa: ANN401 — Rig's own keywords.
    return Rig(tmp_path, gmail, **kwargs)


# --- the menu entry ----------------------------------------------------------------------------


def _tool() -> Tool:
    (tool,) = build_mail_tool(Mailbox(Gmail().tool))
    return tool


def test_mail_is_one_flat_tool_with_a_level_per_action() -> None:
    """Reads are L0, an archive or a draft L1 with no card, a send L3; on the first menu."""
    tool = _tool()
    assert tool.name == "mail"
    assert not tool.deferred
    assert tool.allowed_callers == frozenset({CallerPrincipal.JARVIS_LLM})
    assert (tool.risk_level, tool.read_only, tool.requires_confirmation) == ("L3", False, False)
    assert {one: call_risk(tool, {"action": one}) for one in ACTIONS} == {
        "list": "L0", "search": "L0", "read": "L0", "thread": "L0", "attachment": "L0",
        "draft": "L1", "send": "L3", "archive": "L1",
    }
    assert call_risk(tool, {}) == "L0"  # the default action lists
    assert {one for one in ACTIONS if call_asks(tool, {"action": one})} == {"send"}
    assert only_reads(tool, {"action": "read"})
    assert not only_reads(tool, {"action": "archive", "message": "1"})
    assert not only_reads(tool, {"action": "send"})
    validate_requires_confirmation([tool], "L3")  # the boot check, per action, accepts it


def test_the_description_and_schema_keep_the_tool_standard() -> None:
    """At most 600 characters of English that says "he", names the neighbours; a flat schema."""
    tool = _tool()
    assert len(tool.description) <= 600
    assert tool.description.isascii()
    assert "the user" not in tool.description.lower()
    assert "his" in tool.description
    assert "allen" not in tool.description.lower()
    assert "jobs" in tool.description
    assert "schedule" in tool.description
    schema = tool.input_schema
    assert schema["additionalProperties"] is False
    assert "required" not in schema
    props = schema["properties"]
    assert props["action"]["enum"] == list(ACTIONS)
    assert set(props) == {
        "action", "query", "when", "from", "to", "cc", "bcc", "subject", "body", "reply_to",
        "reply_all", "replaces", "draft", "unread", "waiting", "page", "looking_for", "message",
        "file", "undo",
    }
    assert all(one["description"] for one in props.values())
    assert all(one["type"] in {"string", "integer", "boolean"} for one in props.values())
    assert len(json.dumps(schema)) < 3300  # the schema is part of every request


def test_each_argument_says_which_actions_take_it_and_the_others_refuse_it(rig: Rig) -> None:
    """The description opens with the actions; asking another action for it is an error."""
    props = rig.tool.input_schema["properties"]
    for name, takers in {
        "query": "list, search", "waiting": "list", "file": "attachment", "undo": "archive, draft",
        "message": "read, thread, attachment, archive", "looking_for": "read, attachment",
    }.items():
        assert props[name]["description"].startswith(f"{takers}: ")
    error = rig.fails(action="list", message="1")
    assert error["error"] == "mail: message is for read, thread, attachment or archive, not list"
    assert error["code"] == "invalid_arguments"
    assert rig.fails(action="search", query="x", waiting=True)["error"] == (
        "mail: waiting is for list, not search"
    )
    assert rig.gmail.calls == []  # a refused call asked Gmail nothing


def test_a_name_the_tool_does_not_have_is_an_error_not_a_different_question(rig: Rig) -> None:
    """The old call shape ``mail(read=<id>)`` must not silently list the inbox."""
    error = rig.fails(read="p0")
    assert error["code"] == "invalid_arguments"
    assert "no argument read; its arguments are action, query" in error["error"]
    assert error["kind"] == "retry_other_args"
    assert rig.gmail.calls == []


def test_arguments_are_checked_for_their_type(rig: Rig) -> None:
    """A flag is true or false (or those words); a page is a whole number; the action is known."""
    assert "unread must be true or false" in rig.fails(query="x", unread="maybe")["error"]
    assert "page must be a whole number" in rig.fails(page=0)["error"]
    assert "action must be one of list, search" in rig.fails(action="nope")["error"]
    assert "query must be text" in rig.fails(query={"a": 1})["error"]
    assert rig.ok(action="search", query="plan 2", unread="true")["message"] == "p2"


# --- list --------------------------------------------------------------------------------------


def test_no_arguments_list_his_unread_inbox_counted_exactly_with_what_is_left_out(
    rig: Rig,
) -> None:
    """Every category, eight rows, the count of the rest by category and how to get them."""
    out = rig.ok()
    assert out["summary"] == (
        "14 unread in his inbox, newest first. Showing 1-8. "
        "Not shown: 6 (Primary 1, Promotions 4, Updates 1); page=2 gives the next 8."
    )
    assert [one["n"] for one in out["rows"]] == list(range(1, 9))
    assert [one["message"] for one in out["rows"]] == [
        "u0", "p0", "q0", "p1", "q1", "p2", "q2", "p3",
    ]
    assert rig.searches()[0] == "in:inbox is:unread"  # no category narrows it


def test_a_row_is_the_mails_own_words_and_says_who_wrote_last(rig: Rig) -> None:
    """The opening comes from the cleaned body, not Gmail's snippet; the state from the headers."""
    rows = {one["message"]: one for one in rig.ok()["rows"]}
    assert rows["p0"] == {
        "n": 2, "message": "p0", "from": "Sam Lee", "when": "today (Wed) 10:50 am",
        "subject": "Plan 0", "opening": "Hello, this is plan 0. Details follow.",
        "state": "waiting on him",
    }
    assert rows["q0"]["state"] == "bulk mail"
    assert rows["u0"]["state"] == "automated"
    assert "unread" not in rows["p0"]  # every row of this list is unread: said once, not per row


def test_the_receipt_of_a_list_says_what_it_covered_and_how_to_get_the_rest(rig: Rig) -> None:
    """A read; the window, the rows shown, the six left and the page that has them."""
    result = rig.run()
    receipt = receipt_of(result)
    assert receipt["reads_only"] is True
    assert receipt["line"] == "read mail, unread in his inbox: 14 found, 8 shown"
    assert receipt["covered"] == {
        "sources": ["Gmail"], "window": "mail, unread in his inbox", "shown": 8, "more": 6,
        "how_more": "page=2",
    }


def test_page_two_numbers_on_from_the_first_and_does_not_search_again(rig: Rig) -> None:
    """Row 9 is the ninth of the whole list; the list is held, so the ids are not paged again."""
    rig.ok()
    second = rig.ok(page=2)
    assert second["summary"] == "14 unread in his inbox, newest first. Showing 9-14."
    assert [one["n"] for one in second["rows"]] == list(range(9, 15))
    assert rig.searches().count("in:inbox is:unread") == 1
    assert rig.ok(action="read", message="9")["message"] == "q3"  # row 9, from the second page
    ended = rig.fails(page=3)
    assert ended["error"] == "mail: this list has 14 mails, ending at page 2; page=3 is past it"


def test_asking_for_the_same_list_again_looks_again(rig: Rig) -> None:
    """Only a later page reuses the held list: page 1 is a fresh look at the mailbox."""
    rig.ok()
    rig.gmail.add("new", "Brand new", sender=KIM, to=ME, at=NOW, plain="Just in.")
    again = rig.ok()
    assert again["summary"].startswith("15 unread in his inbox")
    assert again["rows"][0]["message"] == "new"


def test_his_words_become_gmail_terms_at_his_local_midnights(rig: Rig) -> None:
    """``when`` and ``from`` are resolved by the tool: epoch bounds in his zone, a quoted name."""
    out = rig.ok(when="today", **{"from": "Sam"})
    midnight = int(datetime(2026, 10, 21, tzinfo=ZONE).timestamp())
    assert rig.searches()[0] == (
        f'from:"Sam" after:{midnight} before:{midnight + 86400} -in:sent -in:drafts'
    )
    assert out["summary"] == "3 mails, from Sam, today (Wed), newest first."
    assert [one["message"] for one in out["rows"]] == ["p0", "p2", "p4"]
    assert all(one["unread"] for one in out["rows"])  # a list with a window shows read mail too


def test_a_list_nothing_matches_says_what_was_looked_at_and_how_to_look_otherwise(
    tmp_path: Path,
) -> None:
    """An empty inbox and an empty narrowed list are sentences, not empty arrays alone."""
    quiet = _rig(tmp_path, mailbox())
    try:
        assert quiet.ok()["summary"] == "No unread mail in his inbox."
        out = quiet.ok(**{"from": "Nobody"}, when="this week")
        assert out["summary"].startswith("No mail matches from Nobody, Mon Oct 19 to today (Wed).")
        assert "Try fewer words, a wider when or another sender" in out["summary"]
        assert out["rows"] == []
    finally:
        quiet.close()


def test_a_list_past_the_cap_says_more_than_and_that_only_a_narrower_ask_reaches_the_rest(
    tmp_path: Path,
) -> None:
    """With the cap at 10 and 14 unread: "more than 10", and what is not shown is a lower bound."""
    capped = _rig(tmp_path, _morning(), cap=10)
    try:
        out = capped.ok()
        assert out["summary"].startswith("More than 10 unread in his inbox, newest first.")
        assert "Not shown: more than 2" in out["summary"]
        assert "Past the first 10 only a narrower when or from reaches the rest." in out["summary"]
        assert receipt_of(capped.run())["line"].endswith("more than 10 found, 8 shown")
    finally:
        capped.close()


def test_waiting_lists_the_conversations_someone_else_wrote_last_and_says_what_it_left(
    rig: Rig,
) -> None:
    """Five people's mails wait; the shop's and the bank's are bulk or automated, said as such."""
    out = rig.ok(waiting=True)
    assert out["summary"] == (
        "5 threads where someone else wrote last and is waiting on him: Sam Lee (10 minutes ago),"
        " Kim Ray (20 minutes ago), Sam Lee (30 minutes ago). Of 14 conversations with mail in the"
        " last 14 days: 0 end with his own message, 9 bulk, automated or not addressed to him."
    )
    assert [one["message"] for one in out["rows"]] == ["p0", "p1", "p2", "p3", "p4"]
    assert {one["state"] for one in out["rows"]} == {"waiting on him"}
    receipt = receipt_of(rig.run(waiting=True))
    assert receipt["line"].startswith("read mail waiting on him, the last 14 days: 5 found")


def test_a_conversation_he_answered_is_not_waiting_and_the_answer_is_counted(
    tmp_path: Path,
) -> None:
    """His reply ends it, whatever the window held; the summary counts it."""
    gmail = mailbox()
    gmail.add("a1", "Lunch?", sender=SAM, to=ME, thread="ta", at=ago(hours=5))
    gmail.add("a2", "Re: Lunch?", sender=ME, to=SAM, thread="ta", labels=SENT, at=ago(hours=4))
    gmail.add("b1", "Plan", sender=KIM, to=ME, thread="tb", at=ago(days=2))
    solo = _rig(tmp_path, gmail)
    try:
        out = solo.ok(waiting=True)
        assert [one["message"] for one in out["rows"]] == ["b1"]
        assert "1 thread where someone else wrote last" in out["summary"]
        assert "Of 2 conversations with mail in the last 14 days: 1 ends with his own message" in (
            out["summary"]
        )
        assert solo.ok(waiting=True, when="today")["rows"] == []  # nothing today
    finally:
        solo.close()


def test_the_waiting_summary_counts_in_the_singular_and_the_plural() -> None:
    """One ends, two end; one older one is not checked, two older ones are; none is a 0."""
    ask = Ask(query="", window="the last 14 days", unread_only=False, waiting=True)
    now = NOW

    def said(**counts: int) -> str:
        base = {"threads": 9, "answered": 0, "skipped": 0, "unreadable": 0}
        found = Waiting(hits=(), more=False, lasts={}, **{**base, **counts})
        return _waiting_summary(found, ask, now, 500)

    assert said(answered=1, skipped=1, unreadable=1) == (
        "No thread is waiting on him (the last 14 days). Of 9 conversations with mail in the"
        " last 14 days: 1 ends with his own message, 6 bulk, automated or not addressed to"
        " him, 1 older one not checked, 1 could not be read."
    )
    assert said(answered=2, skipped=2) == (
        "No thread is waiting on him (the last 14 days). Of 9 conversations with mail in the"
        " last 14 days: 2 end with his own message, 5 bulk, automated or not addressed to"
        " him, 2 older ones not checked."
    )
    assert said(threads=1) == (
        "No thread is waiting on him (the last 14 days). Of 1 conversation with mail in the"
        " last 14 days: 0 end with his own message, 1 bulk, automated or not addressed to"
        " him."
    )


def test_a_list_stays_within_its_result_budget_with_every_field_at_its_longest(
    tmp_path: Path,
) -> None:
    """Eight rows of the longest sender, subject, opening and file names: whole rows, under 4,000.

    The rows that do not fit are named in the summary, so he can read them by number.
    """
    gmail = mailbox()
    for n in range(10):
        gmail.add(
            f"w{n}", "S" * 300, sender=f"{'N' * 200} <long{n}@example.com>", to=ME,
            at=ago(minutes=n + 1), plain=("word " * 200),
            attachments=[(f"{'f' * 80}{k}.pdf", "application/pdf", pdf(["x"])) for k in range(6)],
        )
    long = _rig(tmp_path, gmail)
    try:
        out = long.ok()
        assert len(json.dumps(out, ensure_ascii=False)) < 4000
        shown = len(out["rows"])
        assert 5 <= shown < 8  # the longest rows do not all fit: whole rows go, never part of one
        assert [one["n"] for one in out["rows"]] == list(range(1, shown + 1))
        numbers = ", ".join(f"message={n}" for n in range(shown + 1, 9))
        assert f"{8 - shown} rows did not fit; read them with {numbers}." in out["summary"]
        assert all(len(one["subject"]) <= 100 and len(one["from"]) <= 40 for one in out["rows"])
        assert all(len(one["files"]) == 4 for one in out["rows"])  # three named, "+3 more"
        receipt = receipt_of(long.run())["covered"]
        assert (receipt["shown"], receipt["more"]) == (shown, 10 - shown)  # all 10 less those shown
    finally:
        long.close()


# --- remembered rows ---------------------------------------------------------------------------


def test_a_rows_number_works_in_a_later_call_and_a_message_id_always_does(
    tmp_path: Path,
) -> None:
    """After a restart (a new tool) a number is an error that says to list again; an id reads."""
    gmail = _morning()
    first = _rig(tmp_path / "one", gmail)
    try:
        first.ok()
        assert first.ok(action="read", message="2")["message"] == "p0"
    finally:
        first.close()
    restarted = _rig(tmp_path / "two", gmail)
    try:
        error = restarted.fails(action="read", message="2")
        assert error["code"] == "no_rows"
        assert error["kind"] == "retry_other_args"
        assert "nothing has been listed since mail was last restarted" in error["error"]
        assert "list or search again, or give the message id" in error["error"]
        assert restarted.ok(action="read", message="p0")["subject"] == "Plan 0"
    finally:
        restarted.close()


def test_the_next_listing_replaces_the_rows_the_last_one_numbered(rig: Rig) -> None:
    """Row 1 of a search is not row 1 of the list before it."""
    assert rig.ok()["rows"][0]["message"] == "u0"
    rig.ok(action="search", query="deal")
    assert rig.ok(action="read", message="1")["message"] == "q0"


def test_rows_may_be_named_as_a_range_a_list_or_ids_and_there_is_no_row_past_the_end(
    rig: Rig,
) -> None:
    """``1-3`` and ``1,2,3`` are rows; an id among them is itself; a missing row is an error."""
    rig.ok()
    by_range = rig.ok(action="read", message="2-4")
    assert [one["message"] for one in by_range["mails"]] == ["p0", "q0", "p1"]
    mixed = rig.ok(action="read", message="1, p4")
    assert [one["message"] for one in mixed["mails"]] == ["u0", "p4"]
    same = rig.ok(action="read", message="2,p0")  # row 2 is p0: one mail, once
    assert same["message"] == "p0"
    assert rig.fails(action="read", message="15")["error"] == (
        "mail: the last listing has 14 rows; there is no row 15"
    )
    assert "takes up to 3 mails" in rig.fails(action="read", message="1-4")["error"]
    assert "takes up to 3 mails at a time, not 4" in rig.fails(
        action="read", message="1,2,3,4",
    )["error"]
    assert READ_MAX == 3


# --- search ------------------------------------------------------------------------------------


def test_a_search_with_one_match_returns_that_mail_read_whole(rig: Rig) -> None:
    """One call, no row to pick: the mail's text, with the sentence that says it was the match."""
    out = rig.ok(action="search", query="plan 2")
    assert out["summary"] == (
        '1 mail matches "plan 2". From Sam Lee, today (Wed) 10:30 am: Plan 2.'
    )
    assert out["text"] == "Hello, this is plan 2.\n\nDetails follow."
    assert out["message"] == "p2"
    assert "rows" not in out
    assert rig.ok(action="read", message="1")["message"] == "p2"  # it is row 1 of its listing


def test_a_search_with_several_matches_is_rows_with_unread_marked_and_all_mail_covered(
    rig: Rig,
) -> None:
    """A search is every mail he has, read or not, so each row says if it is unread."""
    rig.gmail.messages["p1"].labels.discard("UNREAD")
    out = rig.ok(action="search", query="plan")
    assert out["summary"] == '5 mails, "plan", newest first.'
    unread = {one["message"]: one.get("unread", False) for one in out["rows"]}
    assert unread == {"p0": True, "p1": False, "p2": True, "p3": True, "p4": True}
    assert rig.searches()[0] == "plan"  # not limited to the inbox or to received mail


def test_a_search_with_no_match_and_a_search_with_no_terms(rig: Rig) -> None:
    """No match says how to look differently; no terms says what a search needs."""
    out = rig.ok(action="search", query="unicorn")
    assert out["summary"] == (
        'No mail matches "unicorn". Try fewer words, a wider when or another sender.'
    )
    error = rig.fails(action="search")
    assert error["error"] == (
        "mail: search needs query, from, to, when or unread; list with no arguments gives his"
        " unread inbox"
    )
    assert error["code"] == "invalid_arguments"


# --- read --------------------------------------------------------------------------------------


def _long_mail(gmail: Gmail) -> None:
    paragraphs = [f"Paragraph {n}. " + ("The quick brown fox jumps. " * 15) for n in range(1, 25)]
    gmail.add(
        "long", "The long report", sender=KIM, to=ME, at=ago(hours=2),
        plain="\n\n".join(paragraphs),
    )


def test_a_long_mail_is_read_in_parts_that_say_which_part_and_how_to_get_the_next(
    tmp_path: Path,
) -> None:
    """Part 1 of N is cut at a paragraph; page=2 is the next; the receipt says what is left."""
    gmail = mailbox()
    _long_mail(gmail)
    long = _rig(tmp_path, gmail)
    try:
        first = long.ok(action="read", message="long")
        assert first["part"].startswith("part 1 of ")
        parts = int(first["part"].rpartition(" ")[2])
        assert parts >= 2
        assert f"Part 1 of {parts}; page=2 gives the next." in first["summary"]
        assert first["text"].startswith("Paragraph 1.")
        second = long.ok(action="read", message="long", page=2)
        assert second["part"] == f"part 2 of {parts}"
        last_of_first = int(re.findall(r"Paragraph (\d+)\.", first["text"])[-1])
        # The cut falls between paragraphs: the next part opens with the next one.
        assert second["text"].startswith(f"Paragraph {last_of_first + 1}.")
        receipt = receipt_of(long.run(action="read", message="long"))
        assert receipt["line"].endswith(f", part 1 of {parts}")
        assert receipt["covered"]["more"] == parts - 1
        assert receipt["covered"]["how_more"] == "page=2"
        last = long.ok(action="read", message="long", page=parts)
        assert last["summary"].endswith(f"Part {parts} of {parts}.")
        assert long.fails(action="read", message="long", page=parts + 1)["code"] == (
            "invalid_arguments"
        )
    finally:
        long.close()


def test_looking_for_gives_the_opening_and_the_paragraphs_that_match(tmp_path: Path) -> None:
    """What he is looking for picks the paragraphs, in the mail's order, and says what it left."""
    gmail = mailbox()
    paragraphs = [f"Paragraph {n}. " + ("Filler text goes here. " * 18) for n in range(1, 25)]
    paragraphs[17] = "Paragraph 18. The refund of $212.50 will arrive by Friday."
    gmail.add("long", "Report", sender=KIM, to=ME, at=ago(hours=2), plain="\n\n".join(paragraphs))
    long = _rig(tmp_path, gmail)
    try:
        out = long.ok(action="read", message="long", looking_for="refund")
        assert "The refund of $212.50" in out["text"]
        assert out["text"].startswith("Paragraph 1.")
        assert out["showing"].startswith('the opening and the paragraphs that match "refund"')
    finally:
        long.close()


def test_up_to_three_mails_are_read_at_once_each_in_its_share(rig: Rig) -> None:
    """Rows 2 to 4 in one call: each mail whole when short, one summary naming all three."""
    rig.ok()
    out = rig.ok(action="read", message="2,3,4")
    assert [one["message"] for one in out["mails"]] == ["p0", "q0", "p1"]
    assert out["summary"].startswith("Read 3 mails: From Sam Lee, today (Wed) 10:50 am: Plan 0.")
    assert "Each is cut" not in out["summary"]
    assert [one["text"] for one in out["mails"]] == [
        "Hello, this is plan 0.\n\nDetails follow.", "Big sale.",
        "Hello, this is plan 1.\n\nDetails follow.",
    ]
    receipt = receipt_of(rig.run(action="read", message="p0,p1"))
    assert receipt["line"] == "read 2 mails"


def test_three_long_mails_each_get_a_third_and_the_summary_says_so(tmp_path: Path) -> None:
    """Long mails share the result a part would have: each is cut; one read alone has the rest."""
    gmail = mailbox()
    for name in ("one", "two", "three"):
        paragraphs = [f"{name} paragraph {n}. " + ("Word salad. " * 40) for n in range(1, 13)]
        gmail.add(name, f"Report {name}", sender=KIM, to=ME, at=ago(hours=3),
                  plain="\n\n".join(paragraphs))
    three = _rig(tmp_path, gmail)
    try:
        out = three.ok(action="read", message="one,two,three")
        assert "Each is cut to its share; read one alone for the rest." in out["summary"]
        assert all(one["part"].startswith("part 1 of ") for one in out["mails"])
        assert len(json.dumps(out)) < 4400
    finally:
        three.close()


def test_a_message_gmail_has_not_got_names_the_id_and_the_next_step(rig: Rig) -> None:
    """Not found is a failure another argument fixes, and it names the id."""
    error = rig.fails(action="read", message="nope")
    assert error["error"] == "No mail with id nope; list or search first to get one"
    assert (error["code"], error["kind"]) == ("not_found", "retry_other_args")
    assert "No mail with id nope" in rig.fails(action="read", message="p0,nope")["error"]


def test_the_receipt_of_a_read_names_whose_mail_it_was_and_covers_one_message(rig: Rig) -> None:
    """The line she can say; the window is the message; nothing is left out of a short mail."""
    receipt = receipt_of(rig.run(action="read", message="p0"))
    assert receipt["status"] == "done"
    assert receipt["reads_only"] is True
    assert receipt["line"] == 'read Sam Lee\'s mail "Plan 0"'
    assert receipt["covered"] == {
        "sources": ["Gmail"], "window": "one mail, p0", "shown": 1, "more": 0, "how_more": "",
    }


# --- attachment --------------------------------------------------------------------------------


def _invoice(gmail: Gmail) -> None:
    gmail.add(
        "inv", "Invoice 114", sender=KIM, to=ME, at=ago(hours=3), plain="Invoice attached.",
        attachments=[
            ("invoice.pdf", "application/pdf", pdf(["Total due $40.00", "Due Nov 1"])),
            ("logo.png", "image/png", b"\x89PNG" + b"0" * 100),
        ],
    )


def test_an_attachment_is_read_by_number_or_by_part_of_its_name(tmp_path: Path) -> None:
    """A PDF is its text; a picture is named with its type and size; a mail lists its files."""
    gmail = mailbox()
    _invoice(gmail)
    box = _rig(tmp_path, gmail)
    try:
        mail = box.ok(action="read", message="inv")
        assert mail["files"] == ["1. invoice.pdf (PDF, 618 B)", "2. logo.png (PNG, 104 B)"]
        by_number = box.ok(action="attachment", message="inv", file="1")
        assert by_number["text"] == "Total due $40.00\nDue Nov 1"
        assert by_number["summary"] == (
            "invoice.pdf (PDF, 618 B) from Kim Ray, today (Wed) 8:00 am: Invoice 114."
        )
        by_name = box.ok(action="attachment", message="inv", file="logo")
        assert by_name["notice"] == (
            "logo.png is a PNG file: only its name, type and size can be given"
        )
        assert "text" not in by_name
        assert receipt_of(box.run(action="attachment", message="inv", file="1"))["line"] == (
            "read invoice.pdf from a mail"
        )
    finally:
        box.close()


def test_an_attachment_with_no_file_named_lists_them_unless_there_is_only_one(
    tmp_path: Path,
) -> None:
    """Several files need a choice, said with the list; a single file needs none."""
    gmail = mailbox()
    _invoice(gmail)
    memo = pdf(["The meeting moves to Thursday."])
    gmail.add("one", "One file", sender=KIM, to=ME, at=ago(hours=4), plain="See file.",
              attachments=[("memo.pdf", "application/pdf", memo)])
    gmail.add("none", "No files", sender=KIM, to=ME, at=ago(hours=5), plain="Nothing.")
    box = _rig(tmp_path, gmail)
    try:
        error = box.fails(action="attachment", message="inv")
        assert error["error"] == (
            "mail: attachment needs file=<number or name>; its attachments are"
            " 1. invoice.pdf (PDF, 618 B); 2. logo.png (PNG, 104 B)"
        )
        only = box.ok(action="attachment", message="one")
        assert only["text"] == "The meeting moves to Thursday."
        assert box.fails(action="attachment", message="none", file="1")["error"] == (
            "mail: this message has no attachments"
        )
        assert "names none of them" in box.fails(
            action="attachment", message="inv", file="spreadsheet",
        )["error"]
    finally:
        box.close()


def test_an_attachment_of_a_message_too_big_to_read_raw_says_it_was_not_read(
    tmp_path: Path,
) -> None:
    """Past the size limit the files are not downloaded, and asking for one says why."""
    gmail = mailbox()
    _invoice(gmail)
    gmail.messages["inv"].size = 99_000_000
    box = _rig(tmp_path, gmail)
    try:
        error = box.fails(action="attachment", message="inv", file="1")
        assert error["code"] == "attachments_not_read"
        assert error["kind"] == "impossible"
        assert "over the 15.0 MB limit" in error["error"]
        mail = box.ok(action="read", message="inv")
        assert "its attachments were not read" in mail["notice"]
    finally:
        box.close()


# --- thread ------------------------------------------------------------------------------------


def _trip(gmail: Gmail) -> None:
    gmail.add("t1", "Trip plan", sender=SAM, to=ME, thread="T", at=ago(days=3),
              plain="Shall we go Saturday?")
    gmail.add("t2", "Re: Trip plan", sender=ME, to=SAM, thread="T", labels=SENT, at=ago(days=2),
              plain="Saturday works.\n\nOn Sun, Sam Lee wrote:\n> Shall we go Saturday?")
    gmail.add("t3", "Re: Trip plan", sender=SAM, to=ME, thread="T", at=ago(days=1),
              plain="Great, I will book it.\n\nOn Mon, Allen wrote:\n> Saturday works.")


def test_a_conversation_is_its_messages_oldest_first_each_with_its_own_new_text(
    tmp_path: Path,
) -> None:
    """Found by its subject and thread id; quotes cut by structure; the cut is said."""
    gmail = mailbox()
    _trip(gmail)
    box = _rig(tmp_path, gmail)
    try:
        out = box.ok(action="thread", message="t3")
        assert out["summary"] == 'Conversation "Trip plan": 3 messages, oldest first.'
        assert [one["message"] for one in out["messages"]] == ["t1", "t2", "t3"]
        assert [one["from"] for one in out["messages"]] == ["Sam Lee", "Allen", "Sam Lee"]
        assert [one["text"] for one in out["messages"]] == [
            "Shall we go Saturday?", "Saturday works.", "Great, I will book it.",
        ]
        assert out["messages"][1]["left_out"] == ["an older message it quotes"]
        receipt = receipt_of(box.run(action="thread", message="t3"))
        assert receipt["line"] == 'read the conversation "Trip plan": 3 of 3 messages'
        # Its messages are the rows now: row 2 is his reply.
        assert box.ok(action="read", message="2")["from"] == ME
        assert box.searches()[0] == 'subject:"Trip plan"'
    finally:
        box.close()


def test_a_long_conversation_shows_the_newest_six_and_the_earlier_page_needs_no_new_search(
    tmp_path: Path,
) -> None:
    """Nine messages: page 1 is 4-9 and says 3 are earlier; page 2 comes from the held thread."""
    gmail = mailbox()
    for n in range(1, 10):
        gmail.add(f"c{n}", "Re: Budget" if n > 1 else "Budget", sender=SAM if n % 2 else KIM,
                  to=ME, thread="B", at=ago(days=10 - n), plain=f"Message number {n}.")
    box = _rig(tmp_path, gmail)
    try:
        newest = box.ok(action="thread", message="c9")
        assert [one["n"] for one in newest["messages"]] == [4, 5, 6, 7, 8, 9]
        assert newest["summary"] == (
            'Conversation "Budget": 9 messages, oldest first. Showing 4-9. 3 earlier: page=2.'
        )
        searched = len(box.gmail.of("gmail_search"))
        calls = len(box.gmail.calls)
        earlier = box.ok(action="thread", message="c9", page=2)
        assert [one["n"] for one in earlier["messages"]] == [1, 2, 3]
        assert len(box.gmail.calls) == calls  # the held conversation, not read again
        assert len(box.gmail.of("gmail_search")) == searched
        assert receipt_of(box.run(action="thread", message="c9", page=2))["covered"] == {
            "sources": ["Gmail"], "window": 'conversation "Budget"', "shown": 3, "more": 0,
            "how_more": "",
        }
    finally:
        box.close()


def test_a_message_with_no_subject_is_a_conversation_of_itself_and_says_so(
    tmp_path: Path,
) -> None:
    """With nothing to search by, only the message is found, and the answer says that."""
    gmail = mailbox()
    gmail.add("bare", "", sender=SAM, to=ME, thread="Z", plain="Call me.")
    gmail.add("other", "Re: something", sender=KIM, to=ME, thread="Z", plain="Elsewhere.")
    box = _rig(tmp_path, gmail)
    try:
        out = box.ok(action="thread", message="bare")
        assert [one["message"] for one in out["messages"]] == ["bare"]
        assert "It has no subject, so only this message could be found." in out["summary"]
    finally:
        box.close()


# --- archive -----------------------------------------------------------------------------------


def test_archive_takes_two_rows_out_of_the_inbox_in_one_call_and_gives_the_undo(rig: Rig) -> None:
    """One ``gmail_batchModify`` removing INBOX; one event; a receipt with its object and undo."""
    rig.ok()
    result = rig.run(action="archive", message="2,3")
    assert result.error is None
    assert rig.gmail.of("gmail_batchModify") == [
        {"messageIds": ["p0", "q0"], "removeLabelIds": ["INBOX"]},
    ]
    assert "INBOX" not in rig.gmail.labels("p0") | rig.gmail.labels("q0")
    (event,) = list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"]))
    assert event.payload == {
        "message_ids": ["p0", "q0"], "in_inbox": False, "action_id": result.action_id,
    }
    receipt = receipt_of(result)
    assert receipt == {
        "status": "done", "reads_only": False, "line": "took 2 mails out of his inbox",
        "object": "mail:p0", "undo": event.event_uid,
    }
    assert result.payload["undo"] == event.event_uid[:8]  # the model reads the start of it
    assert result.payload["summary"] == (
        'Took 2 mails out of his inbox (Sam Lee: "Plan 0"; Shop: "Deal 0"); they are in All Mail.'
        f" Put them back with action=archive and undo={event.event_uid[:8]}."
    )
    assert ARCHIVE_MAX == 50


def test_only_the_mails_in_the_inbox_are_archived_so_the_undo_restores_exactly_them(
    rig: Rig,
) -> None:
    """One already out of the inbox is said, not changed, and is not in the undo."""
    rig.gmail.messages["p1"].labels.discard("INBOX")
    out = rig.ok(action="archive", message="p0,p1")
    assert out["archived"] == ["p0"]
    assert "1 mail already out of his inbox." in out["summary"]
    assert rig.gmail.of("gmail_batchModify") == [
        {"messageIds": ["p0"], "removeLabelIds": ["INBOX"]},
    ]
    nothing = rig.ok(action="archive", message="p1")
    assert nothing == {"summary": "Nothing to archive: 1 mail already out of his inbox.",
                       "archived": []}
    assert len(rig.gmail.of("gmail_batchModify")) == 1


def test_the_undo_puts_them_back_once_and_a_second_undo_changes_nothing(rig: Rig) -> None:
    """The undo id reverses the archive, the labels are back, and asking again is said, not done."""
    short = rig.ok(action="archive", message="p0,p1")["undo"]
    (archived,) = list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"]))
    uid = archived.event_uid
    back = rig.run(action="archive", undo=short)
    assert back.error is None
    assert back.payload == {"summary": "Put 2 mails back in his inbox.", "restored": ["p0", "p1"]}
    assert rig.gmail.of("gmail_batchModify")[-1] == {
        "messageIds": ["p0", "p1"], "addLabelIds": ["INBOX"],
    }
    assert {"INBOX"} <= rig.gmail.labels("p0") & rig.gmail.labels("p1")
    assert receipt_of(back) == {
        "status": "done", "reads_only": False, "line": "put 2 mails back in his inbox",
        "object": "mail:p0",
    }
    undone = [
        one.payload for one in iter_events_of_types(rig.world.conn, ["mail.inbox_changed"])
    ][-1]
    assert undone == {
        "message_ids": ["p0", "p1"], "in_inbox": True, "action_id": back.action_id, "undoes": uid,
    }
    again = rig.ok(action="archive", undo=short)
    assert again["summary"] == "Those mails were put back in his inbox already; nothing changed."
    assert len(rig.gmail.of("gmail_batchModify")) == 2


def test_an_undo_that_is_not_an_archives_is_refused_and_so_is_one_with_a_message(rig: Rig) -> None:
    """Only the undo an archive gave works, and not together with a message."""
    uid = rig.ok(action="archive", message="p0")["undo"]
    error = rig.fails(action="archive", undo="no-such-event")
    assert error["error"] == (
        "mail: no-such-event does not name one archive; use the undo an archive gave"
    )
    assert error["code"] == "invalid_arguments"
    undone = rig.ok(action="archive", undo=uid)
    assert undone["restored"] == ["p0"]
    other = next(
        one for one in iter_events_of_types(rig.world.conn, ["action.result_observed"])
    ).event_uid
    assert "does not name one archive" in rig.fails(action="archive", undo=other)["error"]
    assert rig.fails(action="archive", undo=uid, message="p1")["error"] == (
        "mail: give undo, or message, not both"
    )
    # An undo is not itself undone: its own event is not an archive's.
    back_event = list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"]))[-1]
    assert "does not name one archive" in rig.fails(
        action="archive", undo=back_event.event_uid,
    )["error"]


def test_the_undo_id_is_eight_characters_and_the_whole_event_id_works_as_well(rig: Rig) -> None:
    """No 32-character id is in what the model reads; a longer start, or capitals, still name it."""
    result = rig.run(action="archive", message="p0")
    assert result.tool_output is not None
    assert not re.search(r"[0-9a-f]{9}", result.tool_output.replace('"p0"', ""))
    short = result.payload["undo"]
    assert re.fullmatch(r"[0-9a-f]{8}", short)
    (archived,) = list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"]))
    assert rig.ok(action="archive", undo=archived.event_uid.upper())["restored"] == ["p0"]
    assert rig.ok(action="archive", message="p1")["archived"] == ["p1"]
    second = [
        one for one in iter_events_of_types(rig.world.conn, ["mail.inbox_changed"])
        if not one.payload["in_inbox"]
    ][-1]
    assert rig.ok(action="archive", undo=second.event_uid[:12])["restored"] == ["p1"]


def test_an_undo_id_shorter_than_eight_characters_or_shared_by_two_archives_is_refused(
    rig: Rig,
) -> None:
    """A start too short to be the one an archive gave names nothing; so does a shared one."""
    short = rig.ok(action="archive", message="p0")["undo"]
    assert "does not name one archive" in rig.fails(action="archive", undo=short[:7])["error"]
    for tail in ("0", "1"):
        emit_event(
            rig.world.conn, type="mail.inbox_changed", event_uid=short + tail * 24,
            payload={"message_ids": ["p1"], "in_inbox": False, "action_id": "A9"},
        )
    shared = rig.fails(action="archive", undo=short)
    assert shared["error"] == (
        f"mail: {short} does not name one archive; use the undo an archive gave"
    )
    assert rig.ok(action="archive", undo=short + "1" * 24)["restored"] == ["p1"]


def test_archiving_what_gmail_has_not_got_is_not_found_and_changes_nothing(rig: Rig) -> None:
    """An id that is nowhere is an error naming it; a few missing among real ones are said."""
    error = rig.fails(action="archive", message="nope")
    assert (error["code"], error["kind"]) == ("not_found", "retry_other_args")
    assert rig.gmail.of("gmail_batchModify") == []
    out = rig.ok(action="archive", message="p0,nope")
    assert out["archived"] == ["p0"]
    assert "1 mail no longer exist in Gmail." in out["summary"]


def test_a_failed_archive_says_mail_could_not_be_changed_and_leaves_no_event(rig: Rig) -> None:
    """The model is told the change failed (not that a read did), and no undo exists."""
    real = rig.gmail.tool

    def breaks_on_change(tool: str, args: Any) -> dict[str, Any]:  # noqa: ANN401 — Gmail's args.
        if tool == "gmail_batchModify":
            msg = "gmail_batchModify: Backend Error"
            raise ToolError(msg, code="mcp_tool_error")
        return real(tool, args)

    rig.box._call = breaks_on_change  # noqa: SLF001 — the fault is in Gmail's call, not the tool.
    error = rig.fails(action="archive", message="p0")
    assert error["code"] == "network_error"
    assert "mail could not be changed just now" in error["error"]
    assert list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"])) == []
    assert "INBOX" in rig.gmail.labels("p0")


def test_archive_is_a_change_for_the_receipts_and_the_live_view_and_a_list_is_a_read(
    rig: Rig,
) -> None:
    """GPT Live's read-only view runs a list and fails an archive with no card and no change."""
    view = ReadOnlyToolRegistry(rig.world.registry)
    assert "mail" in {one.name for one in view.get_definitions()}
    rig.world.lifecycle.register("L1")
    rig.world.lifecycle.transition("L1", "authorized")
    listed = view.dispatch(
        tool_request("mail", "L1", arguments={}), rig.world.conn, rig.world.paths,
        rig.world.lifecycle,
    )
    assert listed.error is None
    rig.world.lifecycle.register("L2")
    rig.world.lifecycle.transition("L2", "authorized")
    refused = view.dispatch(
        tool_request("mail", "L2", arguments={"action": "archive", "message": "p0"}),
        rig.world.conn, rig.world.paths, rig.world.lifecycle,
    )
    assert refused.error == "reads_only_view"
    assert receipt_of(refused)["failure"] == "impossible"
    assert rig.gmail.of("gmail_batchModify") == []
    for n, action in enumerate(("draft", "send"), 3):  # a letter is a change in that view as well
        rig.world.lifecycle.register(f"L{n}")
        rig.world.lifecycle.transition(f"L{n}", "authorized")
        letter = view.dispatch(
            tool_request("mail", f"L{n}", arguments={"action": action, "to": "sam"}),
            rig.world.conn, rig.world.paths, rig.world.lifecycle,
        )
        assert letter.error == "reads_only_view"
    assert list(iter_events_of_types(rig.world.conn, ["mail.inbox_changed"])) == []


# --- when Gmail is not there -------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [{}, {"action": "search", "query": "x"}, {"action": "read", "message": "p0"},
     {"action": "archive", "message": "p0"}],
)
def test_without_gmail_every_action_is_an_error_that_says_so(
    tmp_path: Path, arguments: dict[str, Any],
) -> None:
    """Gmail not connected: an error the model can relay, kind impossible."""

    def not_connected(_tool: str, _args: Any) -> dict[str, Any]:  # noqa: ANN401 — Gmail's args.
        msg = "mcp server 'gmail' is not connected"
        raise LookupError(msg)

    down = _rig(tmp_path, mailbox())
    down.box._call = not_connected  # noqa: SLF001 — the connection is what is missing.
    try:
        error = down.fails(**arguments)
        assert error["error"] == "Gmail is not connected; tell him it needs connecting."
        assert (error["code"], error["kind"]) == ("unavailable", "impossible")
    finally:
        down.close()


# --- the covered check -------------------------------------------------------------------------

READS: dict[str, dict[str, Any]] = {
    "list": {},
    "list_page_two": {"page": 2},
    "list_waiting": {"waiting": True},
    "search": {"action": "search", "query": "plan"},
    "search_none": {"action": "search", "query": "no such words anywhere"},
    "read_one": {"action": "read", "message": "p0"},
    "read_two": {"action": "read", "message": "p0,p1"},
    "read_long_in_parts": {"action": "read", "message": "long"},
    "thread": {"action": "thread", "message": "c3"},
    "attachment": {"action": "attachment", "message": "inv", "file": "1"},
    "attachment_a_picture": {"action": "attachment", "message": "inv", "file": "2"},
}


@pytest.mark.parametrize("name", list(READS))
def test_every_read_receipt_says_what_it_covered_and_the_numbers_agree(
    tmp_path: Path, name: str,
) -> None:
    """The covered check: sources, a window, shown and more that add up, and how to get more."""
    gmail = _morning()
    _invoice(gmail)
    for n in range(1, 4):
        gmail.add(f"c{n}", "Re: Budget" if n > 1 else "Budget", sender=SAM if n % 2 else KIM,
                  to=ME, thread="B", at=ago(days=5 - n), plain=f"Message number {n}.")
    paragraphs = [f"Paragraph {n}. " + ("Word salad. " * 60) for n in range(1, 30)]
    gmail.add("long", "Long report", sender=KIM, to=ME, at=ago(hours=6),
              plain="\n\n".join(paragraphs))
    box = _rig(tmp_path, gmail)
    try:
        args = READS[name]
        if name == "list_page_two":
            box.ok()  # a later page is the held list's
        result = box.run(**args)
        assert result.error is None, result.tool_output
        receipt = receipt_of(result)
        assert receipt["status"] == "done"
        assert receipt["reads_only"] is True
        assert receipt["line"]
        covered = receipt["covered"]
        assert covered["sources"] == ["Gmail"]
        assert covered["window"]  # what was looked at, in words
        assert covered["shown"] >= 0
        assert covered["more"] >= 0
        assert (covered["more"] > 0) == bool(covered["how_more"])  # what is left has a way to it
        if covered["how_more"]:
            assert covered["how_more"].startswith(("page=", "message=", "file="))
    finally:
        box.close()
