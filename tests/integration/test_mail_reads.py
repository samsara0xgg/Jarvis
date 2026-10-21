"""ADR 0362, 0363: a mail, an attachment and a conversation read for the model.

A fake Workspace server (``tests.support.gmail_fake``) holds invented mail as mail programs build
it; each check asserts what the read functions returned. No network, no key.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.mail_gmail import Mailbox, Read
from jarvis.execution.mail_reads import (
    RESULT_CHARS,
    THREAD_READ_MAX,
    THREAD_SHOWN,
    attachment_json,
    choose_file,
    find_thread,
    mail_json,
    subject_of,
    summary_of,
    thread_json,
    view,
)
from jarvis.execution.paragraphs import PART_CHARS
from jarvis.execution.tools import ToolError
from jarvis.state.mail_message import Attachment
from tests.support.gmail_fake import SENT, Gmail
from tests.support.mail_raw import ALEX, SAM, pdf, scanned_pdf

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=ZoneInfo("UTC"))
LIMIT = 15 * 1024 * 1024


def _ago(**span: float) -> datetime:
    return NOW - timedelta(**span)


def _read(gmail: Gmail, message: str, *, limit: int = LIMIT) -> Read:
    return Mailbox(gmail.tool, raw_max_bytes=limit).read(message)


def _json(read: Read, *, page: int = 1, looking_for: str = "") -> dict[str, object]:
    return mail_json(
        read, page=page, looking_for=looking_for, limit=PART_CHARS, raw_max_bytes=LIMIT,
        now=NOW, zone=ZONE,
    )


GMAIL_REPLY = (
    "<div>Thursday works for me.</div><br>"
    '<div class="gmail_quote"><div class="gmail_attr">On Mon, Oct 19, 2026 at 3:04 PM Sam Lee '
    '&lt;sam@example.com&gt; wrote:<br></div><blockquote class="gmail_quote">'
    "Can we meet this week?</blockquote></div>"
)


def test_a_reply_gives_only_its_new_text_and_says_the_quote_was_left_out() -> None:
    """Gmail's quote block goes by its markup; the result says an older message was cut."""
    gmail = Gmail(now=NOW)
    gmail.add("r1", "Re: Meeting", sender=ALEX, to=SAM, html=GMAIL_REPLY, at=_ago(hours=3))
    shown = _json(_read(gmail, "r1"))
    assert shown["text"] == "Thursday works for me."
    assert shown["left_out"] == ["an older message it quotes"]
    assert (shown["from"], shown["subject"]) == ("Alex Wu <alex@example.com>", "Re: Meeting")
    assert shown["to"] == "Sam Lee <sam@example.com>"
    assert shown["when"] == "today (Wed) 8:00 am"
    assert shown["at"] == "2026-10-21T08:00-07:00"
    assert shown["thread"] == "r1"


def test_a_newsletter_that_says_an_analyst_wrote_is_kept_whole() -> None:
    """Prose with "wrote:" in it is content: nothing is cut and nothing is said to be."""
    gmail = Gmail(now=NOW)
    prose = (
        "<p>Markets rose.</p><p>On Wednesday, Wedbush analyst Dan Ives wrote: the rally has "
        "legs.</p><p>More after the break.</p>"
    )
    gmail.add("n1", "Daily brief", html=prose)
    shown = _json(_read(gmail, "n1"))
    assert "the rally has legs." in str(shown["text"])
    assert "More after the break." in str(shown["text"])
    assert "left_out" not in shown


def test_a_forward_with_a_note_above_keeps_the_forwarded_statement() -> None:
    """A forwarded message is content; so is the note above the phone's signature."""
    gmail = Gmail(now=NOW)
    plain = (
        "Can you check this?\n\nSent from my iPhone\n\n"
        "Begin forwarded message:\n\nFrom: Bank <bank@example.com>\nSubject: Statement\n\n"
        "Your balance is $40.00."
    )
    gmail.add("f1", "Fwd: Statement", plain=plain)
    shown = _json(_read(gmail, "f1"))
    assert "Your balance is $40.00." in str(shown["text"])
    assert "Can you check this?" in str(shown["text"])


def _long_body() -> str:
    return "\n\n".join(
        f"Section {n}. " + ("The committee reviewed the item at length. " * 14) for n in range(24)
    )


def test_a_long_body_comes_in_parts_cut_at_paragraphs() -> None:
    """Twelve thousand characters and more: part 1 of N, then the part page names."""
    gmail = Gmail(now=NOW)
    gmail.add("l1", "Minutes", plain=_long_body())
    read = _read(gmail, "l1")
    first = _json(read)
    assert len(read.mail.text) > 12_000
    assert str(first["part"]).startswith("part 1 of ")
    total = int(str(first["part"]).rpartition(" ")[2])
    assert total >= 4
    assert str(first["text"]).startswith("Section 0.")
    third = _json(read, page=3)
    assert third["part"] == f"part 3 of {total}"
    assert str(third["text"]).startswith("Section ")
    assert third["text"] != first["text"]
    for page in range(1, total + 1):
        assert len(json.dumps(_json(read, page=page), ensure_ascii=False)) <= RESULT_CHARS + 100
    with pytest.raises(ToolError) as caught:
        _json(read, page=total + 1)
    assert caught.value.code == "invalid_arguments"


def test_looking_for_gives_the_opening_and_the_paragraph_that_matches() -> None:
    """The paragraph with the amount shows with the opening, in mail order; the rest is counted."""
    gmail = Gmail(now=NOW)
    paragraphs = [f"Intro {n}. " + "Filler words that say little. " * 40 for n in range(20)]
    paragraphs[13] = "The statement balance is $412.50, due on November 3 by preauthorized debit."
    gmail.add("s1", "Statement", plain="\n\n".join(paragraphs))
    shown = _json(_read(gmail, "s1"), looking_for="statement balance due")
    assert "$412.50" in str(shown["text"])
    assert str(shown["text"]).startswith("Intro 0.")
    assert "paragraphs left out" in str(shown["showing"])
    assert "part" not in shown


def test_the_headers_take_room_so_a_result_stays_about_four_thousand_characters() -> None:
    """Many recipients and files shrink the part; the whole result stays near the size."""
    gmail = Gmail(now=NOW)
    many = ", ".join(f"Person {n} <p{n}@example.com>" for n in range(40))
    gmail.add(
        "w1", "Big list " * 20, to=many, plain=_long_body(),
        attachments=[(f"report-{n}.pdf", "application/pdf", pdf([f"Page {n}"])) for n in range(9)],
    )
    shown = _json(_read(gmail, "w1"))
    assert len(json.dumps(shown, ensure_ascii=False)) <= RESULT_CHARS + 100
    assert "(+35 more)" in str(shown["to"])
    assert len(shown["files"]) == 9  # type: ignore[arg-type]


def test_a_message_too_big_to_read_raw_says_its_files_were_not_read() -> None:
    """The text is read in the server's form and the notice says why the attachments were not."""
    gmail = Gmail(now=NOW)
    gmail.add(
        "b1", "Photos", plain="See attached.", size=30_000_000,
        attachments=[("trip.jpg", "image/jpeg", b"\xff\xd8" + b"0" * 800)],
    )
    shown = _json(_read(gmail, "b1"))
    assert shown["text"] == "See attached."
    assert shown["files"] == ["1. trip.jpg (JPG, 802 B)"]
    assert shown["notice"] == (
        "its attachments were not read: the message is 28.6 MB, over the 15.0 MB limit"
    )


def test_a_mail_with_no_text_says_so() -> None:
    """A picture-only mail is "(no text)", not an empty string."""
    gmail = Gmail(now=NOW)
    gmail.add("e1", "Photo", html="<img src='cid:x' width='1' height='1'>")
    assert _json(_read(gmail, "e1"))["text"] == "(no text)"


def test_the_summary_says_whose_mail_it_is_when_and_what_about() -> None:
    """One sentence for the answer's lead: sender, time, subject."""
    gmail = Gmail(now=NOW)
    gmail.add("m1", "Lunch?", sender=SAM, at=_ago(hours=2))
    assert summary_of(_read(gmail, "m1"), NOW, ZONE) == (
        "From Sam Lee, today (Wed) 9:00 am: Lunch?."
    )


# --- attachments -------------------------------------------------------------------------------


def _with_files() -> Gmail:
    gmail = Gmail(now=NOW)
    gmail.add(
        "a1", "Statements", plain="Attached.",
        attachments=[
            ("march-statement.pdf", "application/pdf", pdf([
                "Statement balance  $412.50", "Payment due date  November 3, 2026",
            ])),
            ("scan.pdf", "application/pdf", scanned_pdf()),
            ("notes.csv", "text/csv", b"item,amount\ncoffee,4.50\n"),
            ("photo.jpg", "image/jpeg", b"\xff\xd8" + b"0" * 64),
        ],
    )
    return gmail


def test_a_pdf_attachment_is_read_as_text_with_its_amount_and_due_date() -> None:
    """The statement's numbers are in the PDF only; they reach the model as text."""
    read = _read(_with_files(), "a1")
    one = choose_file(read.mail.attachments, "march")
    shown = attachment_json(
        read, one, page=1, looking_for="", limit=PART_CHARS, raw_max_bytes=LIMIT,
    )
    assert "$412.50" in str(shown["text"])
    assert "November 3, 2026" in str(shown["text"])
    assert (shown["file"], shown["type"]) == ("march-statement.pdf", "PDF")


def test_a_scan_a_picture_and_a_csv_each_say_what_they_are() -> None:
    """A PDF with no text layer is a scan; a picture is only named; a CSV is text."""
    read = _read(_with_files(), "a1")

    def show(name: str) -> dict[str, object]:
        return attachment_json(
            read, choose_file(read.mail.attachments, name), page=1, looking_for="",
            limit=PART_CHARS, raw_max_bytes=LIMIT,
        )

    assert show("scan")["notice"] == (
        "scan.pdf is a scan: it has no text layer, so its words cannot be read"
    )
    assert "text" not in show("scan")
    assert "only its name, type and size" in str(show("photo")["notice"])
    assert "coffee,4.50" in str(show("notes")["text"])


def test_a_file_is_named_by_number_name_or_part_of_a_name_and_a_wrong_one_lists_them() -> None:
    """Number 2, the exact name or a unique part pick it; two matches or none list the files."""
    files = _read(_with_files(), "a1").mail.attachments
    assert choose_file(files, "2").name == "scan.pdf"
    assert choose_file(files, "NOTES.CSV").name == "notes.csv"
    assert choose_file(files, "photo").name == "photo.jpg"
    for wrong in ("pdf", "report", "9"):
        with pytest.raises(ToolError) as caught:
            choose_file(files, wrong)
        assert caught.value.kind == "retry_other_args"
        assert "1. march-statement.pdf" in str(caught.value)
    with pytest.raises(ToolError) as caught:
        choose_file((), "x")
    assert caught.value.code == "not_found"


def test_attachments_that_were_not_read_cannot_be_opened() -> None:
    """Over the size limit the bytes were never downloaded; asking for one is an impossibility."""
    gmail = _with_files()
    gmail.messages["a1"].size = 30_000_000
    read = _read(gmail, "a1")
    with pytest.raises(ToolError) as caught:
        attachment_json(
            read, read.mail.attachments[0], page=1, looking_for="", limit=PART_CHARS,
            raw_max_bytes=LIMIT,
        )
    assert (caught.value.code, caught.value.kind) == ("attachments_not_read", "impossible")
    assert "over the" in str(caught.value)


def test_files_are_listed_with_their_number_type_and_size() -> None:
    """``1. march-statement.pdf (PDF, 1 KB)``: what a read shows and ``attachment`` takes."""
    shown = _json(_read(_with_files(), "a1"))["files"]
    assert isinstance(shown, list)
    assert shown[0].startswith("1. march-statement.pdf (PDF, ")
    assert shown[3].startswith("4. photo.jpg (JPG, ")


# --- the view ----------------------------------------------------------------------------------


def test_a_short_text_is_whole_and_an_empty_one_says_so() -> None:
    """Under the limit nothing is paged or picked, even with a question."""
    assert view("short", page=1, looking_for="x", limit=100) == ("short", {})
    assert view("", page=1, looking_for="", limit=100) == ("(no text)", {})


# --- a conversation ----------------------------------------------------------------------------


def test_a_subject_loses_its_reply_and_forward_prefixes_and_quotes() -> None:
    """Replies and forwards add prefixes in either language; quotes would end a phrase early."""
    assert subject_of("Re: RE: Fwd: Plan") == "Plan"
    assert subject_of("回复：转发： 项目进度") == "项目进度"  # noqa: RUF001 — Chinese mail's colon.
    assert subject_of('He said "yes" (maybe)') == "He said yes (maybe)"
    assert subject_of("Re:") == ""


def _conversation() -> Gmail:
    gmail = Gmail(now=NOW)
    gmail.add("c1", "Dinner on Friday?", sender=SAM, to=ALEX, thread="tc", at=_ago(days=3),
              plain="Are you free Friday night?")
    gmail.add("c2", "Re: Dinner on Friday?", sender=ALEX, to=SAM, thread="tc", labels=SENT,
              at=_ago(days=2), plain="Yes! Where?\n\n> Are you free Friday night?")
    gmail.add("c3", "RE: Re: Dinner on Friday?", sender=SAM, to=ALEX, thread="tc",
              at=_ago(days=1), plain="The new ramen place.")
    gmail.add("x1", "Dinner on Friday?", sender="Kim Ray <kim@example.com>", to=ALEX,
              thread="tx", at=_ago(days=1), plain="A different conversation, same subject.")
    return gmail


def test_a_conversation_is_found_by_its_subject_and_kept_by_thread_oldest_first() -> None:
    """His own reply is in it; another thread with the same subject is not."""
    gmail = _conversation()
    box = Mailbox(gmail.tool)
    thread = find_thread(box, box.read("c3"))
    assert [one.id for one in thread.messages] == ["c1", "c2", "c3"]
    assert (thread.searched, thread.more) == (True, False)
    assert gmail.of("gmail_search")[0]["query"] == 'subject:"Dinner on Friday?"'
    rows, older = thread_json(thread, page=1, now=NOW, zone=ZONE)
    assert older == 0
    assert [(row["n"], row["from"]) for row in rows] == [
        (1, "Sam Lee"), (2, "Alex Wu"), (3, "Sam Lee"),
    ]
    assert rows[1]["text"] == "Yes! Where?"
    assert rows[1]["left_out"] == ["an older message it quotes"]
    assert rows[2]["when"] == "yesterday (Tue) 11:00 am"


def test_a_conversation_whose_subject_has_quotes_punctuation_or_chinese_is_still_found() -> None:
    """The phrase is made safe for Gmail; the thread id does the keeping."""
    for subject in ('Re: He said "yes" (maybe)?', "回复： 项目进度 [v2]", "Fwd: Q3: plan & budget"):  # noqa: RUF001
        gmail = Gmail(now=NOW)
        gmail.add("p1", subject_of(subject) or "x", thread="tp", at=_ago(days=2))
        gmail.add("p2", subject, thread="tp", at=_ago(days=1))
        box = Mailbox(gmail.tool)
        assert [one.id for one in find_thread(box, box.read("p2")).messages] == ["p1", "p2"]


def test_a_message_whose_subject_changed_mid_conversation_is_missed() -> None:
    """The one honest gap: the search is by subject, so a renamed message is not found."""
    gmail = _conversation()
    gmail.add("c4", "Dinner moved to Saturday", sender=SAM, to=ALEX, thread="tc",
              at=_ago(hours=5))
    box = Mailbox(gmail.tool)
    found = find_thread(box, box.read("c3"))
    assert "c4" not in [one.id for one in found.messages]
    assert find_thread(box, box.read("c4")).messages[0].id == "c4"  # its own subject finds itself


def test_a_message_with_no_subject_is_a_conversation_of_itself() -> None:
    """Nothing to search by: no search is made and the result says it was not searched."""
    gmail = Gmail(now=NOW)
    gmail.add("n1", "Re:", thread="tn")
    box = Mailbox(gmail.tool)
    found = find_thread(box, box.read("n1"))
    assert ([one.id for one in found.messages], found.searched) == (["n1"], False)
    assert gmail.of("gmail_search") == []


def test_a_long_conversation_is_read_to_a_cap_newest_first_and_paged_six_at_a_time() -> None:
    """Thirty messages: the newest are read; page 1 shows the last six, page 2 the six before."""
    gmail = Gmail(now=NOW)
    for n in range(30):
        gmail.add(f"t{n:02d}", "Weekly sync" if n == 0 else "Re: Weekly sync", thread="tw",
                  at=_ago(hours=30 - n), plain=f"Notes from week {n}.")
    box = Mailbox(gmail.tool)
    found = find_thread(box, box.read("t29"))
    assert len(found.messages) == THREAD_READ_MAX
    assert found.more
    assert found.messages[-1].id == "t29"
    assert found.messages[0].id == f"t{30 - THREAD_READ_MAX:02d}"
    rows, older = thread_json(found, page=1, now=NOW, zone=ZONE)
    assert [row["message"] for row in rows] == [f"t{n:02d}" for n in range(24, 30)]
    assert [row["n"] for row in rows] == list(range(19, 25))
    assert older == THREAD_READ_MAX - THREAD_SHOWN
    earlier, _ = thread_json(found, page=2, now=NOW, zone=ZONE)
    assert [row["message"] for row in earlier] == [f"t{n:02d}" for n in range(18, 24)]
    with pytest.raises(ToolError) as caught:
        thread_json(found, page=5, now=NOW, zone=ZONE)
    assert caught.value.code == "invalid_arguments"


def test_each_message_of_a_conversation_is_cut_to_its_share_and_says_so() -> None:
    """Six long messages share one result; each shows its start and says how to read it all."""
    gmail = Gmail(now=NOW)
    for n in range(6):
        gmail.add(f"t{n}", "Report" if n == 0 else "Re: Report", thread="tr", at=_ago(hours=10 - n),
                  plain=_long_body())
    box = Mailbox(gmail.tool)
    rows, _ = thread_json(find_thread(box, box.read("t5")), page=1, now=NOW, zone=ZONE)
    assert [row["text_cut"] for row in rows] == [
        f"cut; read it whole with message={n}" for n in range(1, 7)
    ]
    assert len(json.dumps(rows, ensure_ascii=False)) <= RESULT_CHARS - 400


def test_a_file_made_by_hand_is_named_by_its_extension() -> None:
    """The type word is the extension; a name with none uses the MIME subtype."""
    from jarvis.execution.mail_reads import file_lines  # noqa: PLC0415 — one-off helper under test.

    lines = file_lines([
        Attachment("a.xlsx", "application/vnd.ms-excel", 2048),
        Attachment("invite", "text/calendar", 300),
    ])
    assert lines == ["1. a.xlsx (XLSX, 2 KB)", "2. invite (CALENDAR, 300 B)"]
