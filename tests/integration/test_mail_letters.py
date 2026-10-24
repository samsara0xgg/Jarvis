"""ADR 0366 in the `mail` tool: recipients by name, replies, drafts as Gmail drafts, and the send.

The tool runs through the real flat dispatcher over the shared Gmail fake, with invented mail from
example.com senders. Each check asserts what the model is sent, the Gmail calls the tool made, the
rows it left in the event log, and the receipt. No network, no key, no model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution import mail_letters
from jarvis.execution.mail_people import name_matches
from jarvis.state.event_log import iter_events_of_types
from tests.support.gmail_fake import SENT, Gmail
from tests.support.mail_rig import ME, SAM, Rig, ago, mailbox, receipt_of

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

PAT = "Pat Fox <pat@example.com>"
BILLING = "Pat Fox Billing <billing@example.com>"
KIM = "Kim Ray <kim@example.com>"
DANA = "Dana Cole <dana@example.com>"
ALIAS = "Allen <allen@uvic.example.edu>"
WU = "Sam Wu <sam.wu@example.org>"


@pytest.fixture
def gmail() -> Gmail:
    """A mailbox with one old sent message to Sam Lee, so his own address is known."""
    return mailbox()


@pytest.fixture
def rig(tmp_path: Path, gmail: Gmail) -> Iterator[Rig]:
    """The tool over ``gmail``."""
    made = Rig(tmp_path, gmail)
    yield made
    made.close()


def _written(rig: Rig) -> list[Any]:
    return list(iter_events_of_types(rig.world.conn, ["mail.draft_written"]))


def _closed(rig: Rig) -> list[Any]:
    return list(iter_events_of_types(rig.world.conn, ["mail.draft_closed"]))


def _writes(gmail: Gmail) -> list[str]:
    """The Gmail calls that change anything, in order."""
    return [
        name for name in gmail.names()
        if name in {"gmail_createDraft", "gmail_sendDraft", "gmail_modify", "gmail_batchModify"}
    ]


# --- a person he names ---------------------------------------------------------------------------


def test_a_name_is_found_in_his_mail_and_the_result_names_the_last_mail_with_that_person(
    gmail: Gmail, rig: Rig,
) -> None:
    """One Sam in his mail: the draft goes to that address, and the summary says why."""
    out = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    assert out["to"] == ["Sam Lee <sam@example.com>"]
    assert (
        'Sam Lee <sam@example.com> was found in his mail (last mail with them: "Hello",'
    ) in out["summary"]
    (made,) = gmail.of("gmail_createDraft")
    assert made == {"to": "sam@example.com", "subject": "Lunch", "body": "Thursday works."}


def test_the_lookup_is_one_search_of_his_mail_in_the_last_year_in_from_and_to(
    rig: Rig,
) -> None:
    """``(from:"name" OR to:"name") newer_than:1y``, drafts left out; ids first, then heads."""
    rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    assert rig.searches()[-1] == '(from:"Sam" OR to:"Sam") newer_than:1y -in:drafts'


def test_two_people_of_that_name_are_put_to_him_and_nothing_is_written(
    gmail: Gmail, rig: Rig,
) -> None:
    """Needs him: both named with the date of their last mail; Gmail was asked for no draft."""
    gmail.add("wu", "Notes", sender=WU, to=ME, at=ago(days=5))
    error = rig.fails(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    assert (error["code"], error["kind"]) == ("ambiguous_recipient", "needs_him")
    assert 'mail: "Sam" is more than one person in his mail: ' in error["error"]
    assert "Sam Wu <sam.wu@example.org>, last mail " in error["error"]
    assert "Sam Lee <sam@example.com>, last mail " in error["error"]
    assert error["error"].endswith("ask him which, or for the address")
    assert gmail.of("gmail_createDraft") == []
    assert _written(rig) == []


def test_a_fuller_name_picks_the_one_whose_name_has_every_word(gmail: Gmail, rig: Rig) -> None:
    """``Sam Lee`` is only the first Sam; a partial word is not a match (``Sa``)."""
    gmail.add("wu", "Notes", sender=WU, to=ME, at=ago(days=5))
    out = rig.ok(action="draft", to="Sam Lee", subject="Lunch", body="Thursday works.")
    assert out["to"] == ["Sam Lee <sam@example.com>"]
    error = rig.fails(action="draft", to="Sa", subject="Lunch", body="Thursday works.")
    assert error["kind"] == "impossible"


def test_a_name_with_a_comma_in_it_still_goes_to_its_address(gmail: Gmail, rig: Rig) -> None:
    """``"Lee, Kay" <kay@example.com>``: Gmail gets the bare address, the card keeps the name."""
    gmail.add("lee", "Notes", sender='"Lee, Kay" <kay@example.com>', to=ME, at=ago(days=5))
    out = rig.ok(action="draft", to="Kay", subject="Lunch", body="Thursday works.")
    assert out["to"] == ["Lee, Kay <kay@example.com>"]
    assert gmail.of("gmail_createDraft")[-1]["to"] == "kay@example.com"
    done = rig.complete(action="send", draft=out["draft"])
    assert done.arguments is not None
    assert done.arguments["to"] == ["Lee, Kay <kay@example.com>"]


def test_a_name_nobody_has_is_an_error_that_asks_him_for_the_address(
    gmail: Gmail, rig: Rig,
) -> None:
    """Kind impossible, with what to ask; no draft."""
    error = rig.fails(action="draft", to="Dana", subject="Hi", body="Hello.")
    assert (error["code"], error["kind"]) == ("not_found", "impossible")
    assert error["error"] == (
        'mail: no one named "Dana" wrote to him or had a letter from him in the last year; ask'
        " him for the address"
    )
    assert gmail.of("gmail_createDraft") == []


def test_names_match_by_whole_words_with_case_and_accents_folded_and_a_chinese_name_whole() -> None:
    """The match is the code's: ``zoe muller`` is Zo\u00eb M\u00fcller; part of a word is not."""
    assert name_matches("zoe muller", "Zo\u00eb M\u00fcller")
    assert name_matches("MULLER", "Zo\u00eb M\u00fcller")
    assert not name_matches("mull", "Zo\u00eb M\u00fcller")
    assert not name_matches("zoe muller", "Zo\u00eb")
    assert name_matches("\u5c71\u7530\u592a\u90ce", "\u5c71\u7530\u592a\u90ce")
    assert name_matches("\u5c71\u7530", "\u5c71\u7530\u592a\u90ce")  # a Chinese name is a run
    assert not name_matches("\u592a\u90ce\u5c71\u7530", "\u5c71\u7530\u592a\u90ce")
    assert not name_matches("sam", "")


def test_a_sender_whose_name_has_accents_or_is_chinese_is_found_by_the_name_as_written(
    gmail: Gmail, rig: Rig,
) -> None:
    """The display names are read from RFC 2047 headers and matched after folding."""
    gmail.add("z", "Hi", sender="Zo\u00eb M\u00fcller <zoe@example.com>", to=ME, at=ago(days=2))
    gmail.add(
        "y", "Hi", sender="\u5c71\u7530\u592a\u90ce <taro@example.com>", to=ME, at=ago(days=2),
    )
    assert rig.ok(action="draft", to="ZO\u00cb M\u00dcLLER", subject="a", body="b")["to"] == [
        "Zo\u00eb M\u00fcller <zoe@example.com>",
    ]
    assert rig.ok(action="draft", to="\u5c71\u7530\u592a\u90ce", subject="a", body="b")["to"] == [
        "\u5c71\u7530\u592a\u90ce <taro@example.com>",
    ]


def test_a_name_looked_up_is_kept_for_a_day(rig: Rig) -> None:
    """The second letter to the same name searches nothing new."""
    rig.ok(action="draft", to="Sam", subject="One", body="a")
    before = len(rig.searches())
    rig.ok(action="draft", to="sam", subject="Two", body="b")
    assert len(rig.searches()) == before


def test_an_address_he_gives_is_used_as_given_with_no_lookup(gmail: Gmail, rig: Rig) -> None:
    """A bare address, a ``Name <address>`` and a row of both with CJK separators."""
    out = rig.ok(
        action="draft", to="sam@example.com, Kim Ray <kim@example.com>\uff0cdana@example.com",
        subject="Hi", body="Hello.",
    )
    assert out["to"] == ["sam@example.com", "Kim Ray <kim@example.com>", "dana@example.com"]
    assert not any(query.startswith("(from:") for query in rig.searches())
    (made,) = gmail.of("gmail_createDraft")
    assert made["to"] == "sam@example.com, kim@example.com, dana@example.com"


def test_to_takes_a_list_and_cc_and_bcc_go_to_gmail_and_are_shown(gmail: Gmail, rig: Rig) -> None:
    """A list of names and addresses; cc and bcc as comma-joined addresses, each once."""
    out = rig.ok(
        action="draft", to=["Sam", "kim@example.com"], cc="dana@example.com",
        bcc=["kim@example.com", "pat@example.com"], subject="Plan", body="Friday.",
    )
    assert out["to"] == ["Sam Lee <sam@example.com>", "kim@example.com"]
    assert out["cc"] == ["dana@example.com"]
    assert out["bcc"] == ["pat@example.com"]  # kim was in to already
    (made,) = gmail.of("gmail_createDraft")
    assert (made["to"], made["cc"], made["bcc"]) == (
        "sam@example.com, kim@example.com", "dana@example.com", "pat@example.com",
    )


@pytest.mark.parametrize("word", ["me", "Me", "myself", "自己", "我", "我自己"])
def test_me_is_his_own_address_read_from_his_sent_mail_and_marked_you(
    gmail: Gmail, rig: Rig, word: str,
) -> None:
    """No address is in any description: the code reads it from his sent mail."""
    out = rig.ok(action="draft", to=word, subject="Note", body="Remember.")
    assert out["to"] == ["allen@example.com (you)"]
    (made,) = gmail.of("gmail_createDraft")
    assert made["to"] == "allen@example.com"


def test_his_own_name_is_marked_too_and_me_without_any_sent_mail_asks_for_the_address(
    tmp_path: Path,
) -> None:
    """A name that is his own gets (you); with nothing sent there is no address to give."""
    mine = Gmail(now=mailbox().now)
    mine.add("o", "Hi", sender=ME, to=SAM, labels=SENT, at=ago(days=2))
    mine.add("o2", "Hi", sender=SAM, to=ME, at=ago(days=1))
    rig = Rig(tmp_path, mine)
    try:
        assert rig.ok(action="draft", to="Allen", subject="a", body="b")["to"] == [
            "Allen <allen@example.com> (you)",
        ]
        empty = Gmail(now=mine.now)
        lone = Rig(tmp_path / "x", empty)
        try:
            error = lone.fails(action="draft", to="me", subject="a", body="b")
            assert (error["code"], error["kind"]) == ("not_found", "impossible")
            assert "his own address is not known" in error["error"]
        finally:
            lone.close()
    finally:
        rig.close()


def test_none_of_his_addresses_is_in_a_description(rig: Rig) -> None:
    """The tool's text names no address of his: the code finds them."""
    text = rig.tool.description + str(rig.tool.input_schema)
    assert "@" not in text
    assert "allen" not in text.lower()


# --- a reply -------------------------------------------------------------------------------------


def _quote(gmail: Gmail) -> None:
    gmail.add(
        "q1", "Quote 114", sender=PAT, to=f"{ME}, {ALIAS}, {DANA}", cc=KIM, thread="tq",
        at=ago(hours=5), headers={"Reply-To": BILLING}, plain="Please confirm the quote.",
    )
    gmail.add("alias", "Hi", sender=ALIAS, to=SAM, labels=SENT, at=ago(days=3))
    gmail.add("sent1", "Hello again", sender=ME, to=SAM, labels=SENT, at=ago(days=20))


def test_a_reply_goes_to_the_reply_to_address_in_the_mails_thread_with_re_on_its_subject(
    gmail: Gmail, rig: Rig,
) -> None:
    """Reply-To wins over From; the thread id is the mail's; the subject is ``Re: `` and its own."""
    _quote(gmail)
    out = rig.ok(action="draft", reply_to="q1", body="Confirmed.")
    assert out["to"] == ["Pat Fox Billing <billing@example.com>"]
    assert out["subject"] == "Re: Quote 114"
    assert out["answers"].startswith('Pat Fox\'s mail "Quote 114", ')
    (made,) = gmail.of("gmail_createDraft")
    assert made["threadId"] == "tq"
    assert made["to"] == "billing@example.com"
    draft = gmail.drafted()[0].message()
    assert str(draft["In-Reply-To"]) == "<q1@example.com>"  # the server sets it from the thread


def test_a_reply_with_no_reply_to_goes_to_the_sender(gmail: Gmail, rig: Rig) -> None:
    """From is the default; his own mail's reply goes to its To."""
    gmail.add("p", "Plan", sender=PAT, to=ME, thread="tp", at=ago(hours=2))
    assert rig.ok(action="draft", reply_to="p", body="Yes.")["to"] == ["Pat Fox <pat@example.com>"]
    gmail.add("mine", "Plan B", sender=ME, to=KIM, labels=SENT, thread="tm", at=ago(hours=1))
    out = rig.ok(action="draft", reply_to="mine", body="Any news?")
    assert out["to"] == ["Kim Ray <kim@example.com>"]


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Re: Quote 114", "Re: Quote 114"), ("re: quote", "re: quote"),
        ("\u56de\u590d\uff1a\u62a5\u4ef7", "\u56de\u590d\uff1a\u62a5\u4ef7"),
        ("Quote 114", "Re: Quote 114"),
    ],
)
def test_re_is_not_added_twice(gmail: Gmail, rig: Rig, subject: str, expected: str) -> None:
    """A subject that already starts with Re: or its Chinese form is kept."""
    gmail.add("p", subject, sender=PAT, to=ME, thread="tp", at=ago(hours=2))
    assert rig.ok(action="draft", reply_to="p", body="Yes.")["subject"] == expected


def test_reply_all_adds_the_others_without_any_of_his_own_addresses(
    gmail: Gmail, rig: Rig,
) -> None:
    """To and Cc of the mail join the letter's; his two addresses (both in sent mail) are out."""
    _quote(gmail)
    out = rig.ok(action="draft", reply_to="q1", reply_all=True, body="Confirmed.")
    assert out["to"] == ["Pat Fox Billing <billing@example.com>", DANA]
    assert out["cc"] == [KIM]
    (made,) = gmail.of("gmail_createDraft")
    assert (made["to"], made["cc"]) == ("billing@example.com, dana@example.com", "kim@example.com")


def test_an_explicit_to_and_subject_win_over_the_replys_defaults(gmail: Gmail, rig: Rig) -> None:
    """He named the recipient and the subject: the thread alone is the reply's."""
    _quote(gmail)
    out = rig.ok(
        action="draft", reply_to="q1", to="dana@example.com", subject="Forwarding my answer",
        body="See below.",
    )
    assert (out["to"], out["subject"]) == (["dana@example.com"], "Forwarding my answer")
    assert gmail.of("gmail_createDraft")[0]["threadId"] == "tq"


def test_a_row_number_of_the_last_listing_names_the_mail_it_answers(
    gmail: Gmail, rig: Rig,
) -> None:
    """``reply_to=1`` is row 1 of the last list."""
    _quote(gmail)
    rig.ok(action="search", query="quote")
    assert rig.ok(action="draft", reply_to="1", body="Confirmed.")["subject"] == "Re: Quote 114"


def test_reply_all_without_a_mail_to_answer_is_an_error(rig: Rig) -> None:
    """There is nobody to be everyone."""
    error = rig.fails(action="draft", to="Sam", reply_all=True, subject="Hi", body="Hello.")
    assert error["error"] == "mail: reply_all needs reply_to=<the mail it answers>; draft has none"


@pytest.mark.parametrize(
    ("arguments", "said"),
    [
        ({"subject": "Hi", "body": "Hello."}, "mail: draft needs to=<who>, or reply_to=<the mail"),
        ({"to": "sam@example.com", "body": "Hello."}, "mail: a new letter needs a subject"),
        ({"to": "sam@example.com", "subject": "Hi"}, "mail: draft needs body=<the letter's text>"),
        ({"to": "not an address @", "subject": "Hi", "body": "x"}, "is not an address"),
        ({"to": "sam@example.com", "subject": "Hi", "body": "x" * 20001}, "takes up to 20000"),
    ],
)
def test_a_letter_with_a_part_missing_says_which_and_writes_nothing(
    gmail: Gmail, rig: Rig, arguments: dict[str, Any], said: str,
) -> None:
    """Recipient, subject and body are each needed; Gmail is asked for no draft."""
    error = rig.fails(action="draft", **arguments)
    assert said in error["error"]
    assert error["code"] == "invalid_arguments"
    assert gmail.of("gmail_createDraft") == []


# --- a draft -------------------------------------------------------------------------------------


def test_a_draft_is_written_to_gmail_recorded_and_returned_as_the_letter_will_go(
    gmail: Gmail, rig: Rig,
) -> None:
    """The draft's ids, thread and letter in one event; a number, an undo and a receipt."""
    result = rig.run(
        action="draft", to="Sam, me", subject="Lunch", body="Thursday works.\n\nAllen",
    )
    assert result.error is None
    out = dict(result.payload)
    (event,) = _written(rig)
    short = event.event_uid[:8]
    assert (out["draft"], out["undo"]) == (short, short)
    assert out["to"] == ["Sam Lee <sam@example.com>", "allen@example.com (you)"]
    assert out["body"] == "Thursday works.\n\nAllen"
    assert "answers" not in out
    assert out["summary"].startswith(f"Wrote draft {short} in his Gmail drafts; nothing was sent.")
    assert out["summary"].endswith(
        f"Send it with action=send and draft={short}; discard it with action=draft and"
        f" undo={short}.",
    )
    payload = event.payload
    assert (payload["draft_id"], payload["message_id"], payload["thread_id"]) == ("r1", "d1", "d1")
    assert payload["letter"] == {
        "to": ["Sam Lee <sam@example.com>", "allen@example.com"], "cc": [], "bcc": [],
        "subject": "Lunch", "body": "Thursday works.\n\nAllen", "thread": "",
    }
    row = receipt_of(result)
    assert (row["status"], row["object"], row["undo"]) == (
        "done", f"draft:{short}", event.event_uid,
    )
    assert row["reads_only"] is False
    assert row["line"] == "wrote a draft to Sam Lee, allen@example.com: Lunch"
    assert [one.id for one in gmail.drafted()] == ["d1"]
    assert _writes(gmail) == ["gmail_createDraft"]  # a draft sends nothing


def test_a_long_body_is_shown_cut_but_the_draft_holds_all_of_it(gmail: Gmail, rig: Rig) -> None:
    """The result stays small: 2,000 characters of body, and a sentence that says it is cut."""
    body = "A line of the letter.\n" * 300
    out = rig.ok(action="draft", to="Sam", subject="Long", body=body)
    assert len(out["body"]) == mail_letters.BODY_SHOWN
    assert "The body shown is cut at 2000 characters; the draft has it all." in out["summary"]
    assert gmail.of("gmail_createDraft")[0]["body"] == body.strip()


def test_a_revision_replaces_the_draft_keeps_what_is_not_given_and_trashes_the_old_one(
    gmail: Gmail, rig: Rig,
) -> None:
    """New draft, old message to Trash by ``gmail_modify``, one closing row naming the old."""
    first = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    out = rig.ok(action="draft", replaces=first["draft"], body="Friday works better.")
    assert out["to"] == ["Sam Lee <sam@example.com>"]
    assert (out["subject"], out["body"]) == ("Lunch", "Friday works better.")
    assert out["replaced"] == first["draft"]
    assert f"It replaces draft {first['draft']}, which was moved to Trash." in out["summary"]
    assert gmail.of("gmail_modify") == [{"messageId": "d1", "addLabelIds": ["TRASH"]}]
    assert [one.id for one in gmail.drafted()] == ["d2"]
    new, old = _written(rig)[1], _written(rig)[0]
    assert new.payload["replaces"] == old.event_uid
    (closed,) = _closed(rig)
    assert closed.payload["draft_id"] == "r1"
    assert closed.payload["how"] == "replaced"
    assert "in_drafts" not in closed.payload


def test_a_revision_of_a_reply_stays_in_its_thread(gmail: Gmail, rig: Rig) -> None:
    """The replaced draft's reply carries over, so the new one has its thread and recipients."""
    _quote(gmail)
    first = rig.ok(action="draft", reply_to="q1", body="Confirmed.")
    rig.ok(action="draft", replaces=first["draft"], body="Confirmed, thanks.")
    second = gmail.of("gmail_createDraft")[1]
    assert (second["threadId"], second["to"], second["subject"]) == (
        "tq", "billing@example.com", "Re: Quote 114",
    )


def test_where_gmail_will_not_trash_the_old_draft_the_result_says_it_is_still_in_drafts(
    gmail: Gmail, rig: Rig,
) -> None:
    """The new draft is written; the old one is closed as replaced with ``in_drafts``."""
    first = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    gmail.fail["gmail_modify"] = "Insufficient permissions"
    out = rig.ok(action="draft", replaces=first["draft"], body="Friday.")
    assert (
        f"It replaces draft {first['draft']}, which is still in his Drafts: Gmail would not move"
        " it to Trash."
    ) in out["summary"]
    assert _closed(rig)[0].payload["in_drafts"] is True
    assert {one.id for one in gmail.drafted()} == {"d1", "d2"}


def test_the_undo_discards_the_draft_once_and_says_so_the_second_time(
    gmail: Gmail, rig: Rig,
) -> None:
    """``draft`` with ``undo``: the message to Trash, a closing row that names the draft."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    result = rig.run(action="draft", undo=made["undo"])
    assert result.error is None
    assert result.payload["summary"] == f"Discarded draft {made['draft']}: moved to Trash."
    assert gmail.drafted() == []
    (closed,) = _closed(rig)
    assert (closed.payload["how"], closed.payload["undoes"]) == (
        "discarded", _written(rig)[0].event_uid,
    )
    assert receipt_of(result)["object"] == f"draft:{made['draft']}"
    again = rig.ok(action="draft", undo=made["undo"])
    assert again["summary"] == f"Draft {made['draft']} was discarded; nothing changed."
    assert len(gmail.of("gmail_modify")) == 1


def test_a_draft_gmail_will_not_trash_stays_open_and_the_undo_says_so(
    gmail: Gmail, rig: Rig,
) -> None:
    """Nothing is recorded as discarded, because the draft is still there to be sent."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    gmail.fail["gmail_modify"] = "Insufficient permissions"
    error = rig.fails(action="draft", undo=made["undo"])
    assert error["error"] == (
        f"mail: Gmail would not move draft {made['draft']} to Trash, so it is still in his"
        " Drafts; tell him to delete it there"
    )
    assert _closed(rig) == []


def test_an_undo_names_one_draft_and_stands_alone(rig: Rig) -> None:
    """An id that matches none, and an undo with other arguments, are refused."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    assert rig.fails(action="draft", undo="deadbeef")["error"] == (
        "mail: deadbeef does not name one of his drafts; use the draft number a draft gave"
    )
    assert rig.fails(action="draft", undo=made["undo"], body="x")["error"] == (
        "mail: give undo alone to discard a draft"
    )


def test_a_draft_that_was_replaced_cannot_be_replaced_or_sent_and_names_its_successor(
    rig: Rig,
) -> None:
    """The ledger says how each ended; the model is told which draft stands."""
    first = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    second = rig.ok(action="draft", replaces=first["draft"], body="Friday.")
    error = rig.fails(action="draft", replaces=first["draft"], body="Saturday.")
    assert error["error"] == (
        f"mail: draft {first['draft']} was replaced by draft {second['draft']}; there is nothing"
        " to replace"
    )
    assert f"draft {first['draft']} was replaced by draft {second['draft']}" in (
        rig.complete(action="send", draft=first["draft"]).error
    )


def test_the_ledger_is_the_event_log_so_a_draft_is_known_without_the_tools_memory(
    rig: Rig,
) -> None:
    """``drafts`` reads the rows alone: a restart or another device finds the same drafts."""
    first = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    rig.ok(action="draft", replaces=first["draft"], body="Friday.")
    held = mail_letters.drafts(rig.world.conn)
    assert [(one.short == first["draft"], one.ended) for one in held] == [
        (True, "replaced"), (False, ""),
    ]
    assert held[1].letter.body == "Friday."
    assert mail_letters.open_in(rig.world.conn, "d2") == held[1]
    assert mail_letters.open_in(rig.world.conn, "d1") is None


# --- send ----------------------------------------------------------------------------------------


def test_a_sends_completion_is_the_letter_with_names_made_addresses_and_only_reads(
    gmail: Gmail, rig: Rig,
) -> None:
    """Flat: ``to`` and the rest as ``Name <address>``, the thread, the draft and which are his."""
    _quote(gmail)
    done = rig.complete(
        action="send", reply_to="q1", reply_all=True, cc="me", body="Confirmed.",
    )
    assert done.error == ""
    assert done.arguments == {
        "action": "send",
        "to": ["Pat Fox Billing <billing@example.com>", DANA],
        "cc": ["allen@example.com", KIM],
        "bcc": [],
        "subject": "Re: Quote 114",
        "body": "Confirmed.",
        "thread": "tq",
        "draft": "",
        "you": ["allen@example.com"],
    }
    assert _writes(gmail) == []  # no Gmail write before the card is answered
    assert _written(rig) == []


def test_a_sends_completion_from_a_draft_is_that_drafts_letter_and_its_number(
    rig: Rig,
) -> None:
    """``draft=<number>`` becomes the draft's letter."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    done = rig.complete(action="send", draft=made["draft"])
    assert done.arguments == {
        "action": "send", "to": ["Sam Lee <sam@example.com>"], "cc": [], "bcc": [],
        "subject": "Lunch", "body": "Thursday works.", "thread": "", "draft": made["draft"],
        "you": [],
    }


def test_a_completion_that_cannot_resolve_a_name_makes_no_card_and_says_what_to_ask(
    rig: Rig,
) -> None:
    """The failure is the completion's: the kind the name's problem has."""
    done = rig.complete(action="send", to="Dana", subject="Hi", body="Hello.")
    assert done.arguments is None
    assert done.kind == "impossible"
    assert "ask him for the address" in done.error


def test_a_send_takes_a_draft_or_a_letter_and_the_model_cannot_fill_what_the_tool_completes(
    rig: Rig,
) -> None:
    """Not both; ``thread`` and ``you`` are not arguments of the public call."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    error = rig.complete(action="send", draft=made["draft"], body="x")
    assert error.error == (
        "mail: send takes draft=<number>, or a letter (to, subject, body), not both"
    )
    forged = rig.complete(action="send", to="sam@example.com", subject="a", body="b", thread="t9")
    assert forged.arguments is None
    assert "no argument thread; its arguments are action, " in forged.error
    assert rig.complete(action="send", to="sam@example.com", subject="a", body="b", you=["x"]).error


def test_a_send_with_no_lease_is_refused_at_l4_and_writes_nothing(gmail: Gmail, rig: Rig) -> None:
    """The L3 call does not run without the lease a card's accept gives."""
    refused = rig.fails(action="send", to="sam@example.com", subject="Hi", body="Thursday")
    assert (refused["code"], refused["kind"]) == ("no_lease", "dont_retry")
    assert _writes(gmail) == []


def test_a_sent_letter_is_one_new_draft_and_one_send_and_is_done_only_with_gmails_answer(
    gmail: Gmail, rig: Rig,
) -> None:
    """After the lease: a new draft, then its send, once each; the object is mail:<id>."""
    done = rig.complete(action="send", to="Sam", subject="Lunch", body="Thursday works.")
    assert done.arguments is not None
    result = rig.leased(**done.arguments)
    assert result.error is None
    assert _writes(gmail) == ["gmail_createDraft", "gmail_sendDraft"]
    assert gmail.of("gmail_sendDraft") == [{"draftId": "r1"}]
    assert gmail.labels("d1") == {"SENT"}
    assert result.payload == {
        "summary": 'Sent "Lunch" to Sam Lee.', "sent": "d1", "to": ["Sam Lee <sam@example.com>"],
    }
    row = receipt_of(result)
    assert (row["status"], row["object"], row["line"]) == (
        "done", "mail:d1", "sent Lunch to Sam Lee",
    )
    assert row["undo"] is None if "undo" in row else True
    (closed,) = _closed(rig)
    assert (closed.payload["how"], closed.payload["message_id"]) == ("sent", "d1")


def test_a_draft_sent_unchanged_is_sent_as_it_is_with_no_new_draft(gmail: Gmail, rig: Rig) -> None:
    """The letter equals the draft's: ``gmail_sendDraft`` on it and nothing else."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    done = rig.complete(action="send", draft=made["draft"])
    assert done.arguments is not None
    assert rig.leased(**done.arguments).error is None
    assert _writes(gmail) == ["gmail_createDraft", "gmail_sendDraft"]  # the draft's, then its send
    assert len(gmail.of("gmail_createDraft")) == 1
    assert gmail.labels("d1") == {"SENT"}
    assert [one.payload["how"] for one in _closed(rig)] == ["sent"]


def test_a_letter_he_edited_on_the_card_is_what_is_sent_in_a_new_draft_that_replaces_the_old(
    gmail: Gmail, rig: Rig,
) -> None:
    """The card's edit changes ``body``; the old draft goes to Trash after the send."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    done = rig.complete(action="send", draft=made["draft"])
    assert done.arguments is not None
    edited = {**done.arguments, "body": "Thursday works. Bring the notes.", "subject": "Lunch?"}
    result = rig.leased(**edited)
    assert result.error is None
    assert [call["subject"] for call in gmail.of("gmail_createDraft")] == ["Lunch", "Lunch?"]
    assert gmail.of("gmail_createDraft")[1]["body"] == "Thursday works. Bring the notes."
    assert gmail.of("gmail_sendDraft") == [{"draftId": "r2"}]
    assert gmail.labels("d2") == {"SENT"}
    assert "TRASH" in gmail.labels("d1")
    assert [(one.payload["draft_id"], one.payload["how"]) for one in _closed(rig)] == [
        ("r2", "sent"), ("r1", "replaced"),
    ]
    assert _written(rig)[1].payload["replaces"] == _written(rig)[0].event_uid


def test_a_send_to_himself_shows_his_address_marked_and_goes_to_it(gmail: Gmail, rig: Rig) -> None:
    """``to="me"``: the completed letter names his address and ``you`` carries it."""
    done = rig.complete(action="send", to="me", subject="Test", body="A test.")
    assert done.arguments is not None
    assert (done.arguments["to"], done.arguments["you"]) == (
        ["allen@example.com"], ["allen@example.com"],
    )
    assert rig.leased(**done.arguments).error is None
    assert gmail.of("gmail_createDraft")[0]["to"] == "allen@example.com"


def test_a_failed_send_leaves_no_draft_this_call_wrote_and_says_so(
    gmail: Gmail, rig: Rig,
) -> None:
    """The new draft is moved to Trash and closed; the failure says the letter was not sent."""
    done = rig.complete(action="send", to="Sam", subject="Lunch", body="Thursday works.")
    assert done.arguments is not None
    gmail.fail["gmail_sendDraft"] = "Backend Error"
    result = rig.leased(**done.arguments)
    assert result.error is not None
    assert receipt_of(result)["status"] == "failed"
    assert gmail.drafted() == []
    assert "TRASH" in gmail.labels("d1")
    assert "the letter was not sent, and the draft this call wrote was moved to Trash." in (
        str(result.tool_output)
    )
    assert [one.payload["how"] for one in _closed(rig)] == ["discarded"]


def test_a_failed_send_of_a_draft_leaves_that_draft_where_it_is(gmail: Gmail, rig: Rig) -> None:
    """The draft he named stays in Drafts, and is still open for another try."""
    made = rig.ok(action="draft", to="Sam", subject="Lunch", body="Thursday works.")
    done = rig.complete(action="send", draft=made["draft"])
    assert done.arguments is not None
    gmail.fail["gmail_sendDraft"] = "Backend Error"
    result = rig.leased(**done.arguments)
    assert f"draft {made['draft']} is still in his Drafts." in str(result.tool_output)
    assert [one.id for one in gmail.drafted()] == ["d1"]
    assert _closed(rig) == []


def test_an_answer_lost_after_gmail_sent_is_found_out_from_the_drafts_labels(
    gmail: Gmail, rig: Rig,
) -> None:
    """The send happened, the answer did not come back: SENT on the message makes it done."""
    done = rig.complete(action="send", to="Sam", subject="Lunch", body="Thursday works.")
    assert done.arguments is not None
    gmail.lose.add("gmail_sendDraft")
    result = rig.leased(**done.arguments)
    assert result.error is None
    assert receipt_of(result)["object"] == "mail:d1"
    assert gmail.labels("d1") == {"SENT"}


def test_when_it_cannot_be_told_whether_the_letter_went_nothing_is_trashed_and_he_is_asked(
    gmail: Gmail, rig: Rig,
) -> None:
    """Gmail cannot say: no Trash for a message that may be the sent one; dont_retry."""
    done = rig.complete(action="send", to="Sam", subject="Lunch", body="Thursday works.")
    assert done.arguments is not None
    gmail.fail |= {"gmail_sendDraft": "Backend Error", "gmail_get": "Backend Error"}
    result = rig.leased(**done.arguments)
    assert receipt_of(result)["failure"] == "dont_retry"
    assert "it is not known whether the letter went" in str(result.tool_output)
    assert gmail.of("gmail_modify") == []


def test_a_send_with_a_lease_and_the_public_form_is_completed_then_sent(
    gmail: Gmail, rig: Rig,
) -> None:
    """A direct dispatch with a lease (no decision loop) makes the same letter."""
    result = rig.leased(action="send", to="Sam", subject="Lunch", body="Thursday works.")
    assert result.error is None
    assert gmail.of("gmail_createDraft")[0]["to"] == "sam@example.com"
    assert len(gmail.of("gmail_sendDraft")) == 1


def test_a_reply_is_sent_in_its_thread(gmail: Gmail, rig: Rig) -> None:
    """The completed ``thread`` becomes ``threadId`` on the new draft."""
    _quote(gmail)
    done = rig.complete(action="send", reply_to="q1", body="Confirmed.")
    assert done.arguments is not None
    assert rig.leased(**done.arguments).error is None
    assert gmail.of("gmail_createDraft")[0]["threadId"] == "tq"


def test_the_completed_form_is_checked_when_it_runs(gmail: Gmail, rig: Rig) -> None:
    """A frozen letter whose recipient became text, or lost its subject, is refused."""
    base = {
        "action": "send", "to": ["sam@example.com"], "cc": [], "bcc": [], "subject": "Hi",
        "body": "Hello.", "thread": "", "draft": "", "you": [],
    }
    assert rig.leased(**{**base, "to": ["Sam"]}).error is not None
    assert rig.leased(**{**base, "subject": " "}).error is not None
    assert rig.leased(**{**base, "to": []}).error is not None
    assert rig.leased(**{**base, "body": 3}).error is not None
    assert _writes(gmail) == []
