"""ADR 0366: how a letter the mail tool completed reads on a card and in its question.

Pure text: the zh/en table, no model, no network. Every address is on example.com.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from jarvis.shared import lang
from jarvis.shared.text import split_person

if TYPE_CHECKING:
    from collections.abc import Iterator

SAM = "Sam Lee <sam@example.com>"
PAT = "Pat Fox <pat@example.com>"
ME = "allen@example.com"


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    """Each test starts in English and leaves the language as it was."""
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


def _letter(**more: Any) -> dict[str, Any]:  # noqa: ANN401 — the fields a test changes.
    """What the mail tool freezes for a send: the letter, completed."""
    return {
        "action": "send", "to": [SAM], "cc": [], "bcc": [], "subject": "Lunch",
        "body": "Thursday works.", "thread": "", "draft": "", "you": [],
    } | more


def test_a_completed_letter_lists_each_recipient_by_name_and_address_with_his_own_marked() -> None:
    """To first, then Cc and Bcc as labelled entries; (you) after any address that is his."""
    card = lang.letter_card(_letter(to=[SAM, ME], cc=[PAT], bcc=[ME], you=[ME]))
    assert card == {
        "to": [SAM, f"{ME} (you)", f"Cc {PAT}", f"Bcc {ME} (you)"],
        "subject": "Lunch", "body": "Thursday works.",
    }


def test_a_reply_card_carries_the_thread_for_the_clients_reply_mark() -> None:
    """``threadId`` only when the letter answers a conversation."""
    assert "threadId" not in lang.letter_card(_letter())
    assert lang.letter_card(_letter(thread="t9"))["threadId"] == "t9"


def test_the_card_is_in_the_language_he_reads() -> None:
    """The mark and the labels come from the table."""
    lang.set_language("zh")
    card = lang.letter_card(_letter(to=[ME], cc=[PAT], bcc=[SAM], you=[ME]))
    assert card["to"] == [f"{ME}（你）", f"抄送 {PAT}", f"密送 {SAM}"]  # noqa: RUF001
    assert lang.letter_card(_letter(to=[ME], you=[ME]), "en")["to"] == [f"{ME} (you)"]


def test_a_call_that_is_not_a_completed_letter_is_shown_as_it_is() -> None:
    """A letter with a plain ``to`` string, or any other tool's arguments, is not rewritten."""
    plain = {"to": "sam@example.com", "subject": "Hi", "body": "Hello"}
    assert lang.letter_card(plain) == plain
    assert lang.letter_card({"light": "lamp", "on": False}) == {"light": "lamp", "on": False}
    assert lang.letter_card({"to": [SAM], "subject": "Hi", "body": "x"}) == {
        "to": [SAM], "subject": "Hi", "body": "x",
    }  # no list of his own addresses: not the mail tool's


def test_the_question_names_each_person_by_name_and_says_who_is_he_and_who_is_copied() -> None:
    """The name, else the address; (you) after his own; Cc and Bcc after the people."""
    one = lang.ask_line("mail", _letter())
    assert one == 'The email to Sam Lee is ready, subject "Lunch". Send it?'
    many = lang.ask_line("mail", _letter(to=[SAM, ME], cc=[PAT], bcc=[SAM], you=[ME]))
    assert many == (
        'The email to Sam Lee, allen@example.com (you) (Cc Pat Fox) (Bcc Sam Lee) is ready,'
        ' subject "Lunch". Send it?'
    )
    lang.set_language("zh")
    assert lang.ask_line("mail", _letter(to=[SAM, ME], cc=[PAT], you=[ME])) == (
        "信写好了，发给 Sam Lee、allen@example.com（你）（抄送 Pat Fox），主题「Lunch」。要发吗？"  # noqa: RUF001
    )


def test_an_older_form_of_a_letter_keeps_its_question() -> None:
    """A ``to`` string or list of text is joined as before."""
    assert lang.letter_to({"to": "sam@example.com", "subject": "Hi", "body": "x"}) == (
        "sam@example.com"
    )
    two = {"to": ["a@example.com", "b@example.com"], "subject": "Hi", "body": "x"}
    assert lang.letter_to(two) == "a@example.com, b@example.com"
    assert lang.letter_to({"subject": "Hi"}) is None


def test_what_the_mail_tool_confirms_is_named_a_send() -> None:
    """The card's action and the line after it."""
    assert lang.action("mail") == ("send this email", "Email sent")
    lang.set_language("zh")
    assert lang.action("mail") == ("发这封邮件", "邮件已发出")


def test_a_name_with_a_comma_is_one_name() -> None:
    """``Lee, Sam <address>`` splits at the last angle bracket, as ``parseaddr`` cannot."""
    assert split_person("Lee, Sam <lee@example.com>") == ("Lee, Sam", "lee@example.com")
    assert split_person('"Lee, Sam" <lee@example.com>') == ("Lee, Sam", "lee@example.com")
    assert split_person("lee@example.com") == ("", "lee@example.com")
    assert split_person("<lee@example.com>") == ("", "lee@example.com")
    assert lang.ask_line("mail", _letter(to=["Lee, Sam <lee@example.com>"])).startswith(
        "The email to Lee, Sam is ready",
    )
