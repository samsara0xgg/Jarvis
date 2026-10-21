"""ADR 0364: a list counts exactly, names what it left out and says who wrote last.

A fake Workspace server (``tests.support.gmail_fake``) holds invented mail; each check asserts what
the list functions returned or what they asked Gmail for. No network, no key.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_lists import (
    PAGE_ROWS,
    WAITING_CHECKED,
    Ask,
    Last,
    ask_of,
    find_waiting,
    last_writers,
    newest_by_thread,
    not_shown,
    opening,
    page_rows,
    row_json,
    state_line,
    waiting_line,
)
from jarvis.execution.tools import ToolError
from tests.support.gmail_fake import PRIMARY, PROMOTIONS, SENT, UPDATES, Gmail, stamp
from tests.support.mail_raw import PADDING

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 21, 18, 0, tzinfo=ZoneInfo("UTC"))  # 11:00 on Wednesday in Vancouver
ME = "Allen <allen@example.com>"
SAM = "Sam Lee <sam@example.com>"
KIM = "Kim Ray <kim@example.com>"
NEWS = "Weekly Brew <hello@brew.example.com>"


def _ago(**span: float) -> datetime:
    return NOW - timedelta(**span)


def _mailbox() -> Gmail:
    """A mailbox where one sent message tells which address is his."""
    gmail = Gmail(now=NOW)
    gmail.add("sent0", "Hello", sender=ME, to=SAM, labels=SENT, at=_ago(days=40))
    return gmail


def _threads(gmail: Gmail) -> Gmail:
    """Five conversations, newest listed first, each in a different state."""
    gmail.add("a1", "Lunch on Friday?", sender=SAM, to=ME, thread="ta", at=_ago(hours=5))
    gmail.add("a2", "Re: Lunch on Friday?", sender=ME, to=SAM, thread="ta", labels=SENT,
              at=_ago(hours=4))
    gmail.add("b1", "Draft of the plan", sender=KIM, to=ME, thread="tb", at=_ago(days=2))
    gmail.add("c1", "This week in coffee", sender=NEWS, to=ME, thread="tc", at=_ago(hours=3),
              headers={"List-Unsubscribe": "<mailto:u@brew.example.com>"})
    gmail.add("d1", "Your login alert", sender="Security <noreply@example.com>", to=ME,
              thread="td", at=_ago(hours=2), headers={"Auto-Submitted": "auto-generated"})
    gmail.add("e1", "Team update", sender=KIM, to="Team <team@example.com>", cc=ME, thread="te",
              at=_ago(hours=1))
    return gmail


# --- what he asked -----------------------------------------------------------------------------


def test_no_arguments_ask_for_his_unread_inbox_in_every_category() -> None:
    """The default list is unread mail in the inbox, with no category in the query."""
    ask = ask_of({}, NOW, ZONE, searching=False)
    assert ask.query == "in:inbox is:unread"
    assert (ask.window, ask.unread_only, ask.waiting) == ("unread in his inbox", True, False)


def test_a_day_in_his_words_is_bounded_by_his_local_midnights_and_leaves_out_sent_mail() -> None:
    """「今天」 at 11:00 in Vancouver is midnight to midnight there, in epoch seconds."""
    ask = ask_of({"when": "今天"}, NOW, ZONE, searching=False)
    start = int(datetime(2026, 10, 21, 0, 0, tzinfo=ZONE).timestamp())
    assert ask.query == f"after:{start} before:{start + 86400} -in:sent -in:drafts"
    assert (ask.window, ask.unread_only, ask.since) == ("today (Wed)", False, start)
    # The day the clocks go back is 25 hours long where they do.
    east = ZoneInfo("America/New_York")
    long_day = ask_of({"when": "2026-11-01"}, datetime(2026, 11, 2, tzinfo=east), east,
                      searching=False)
    first, _, rest = long_day.query.partition(" before:")
    assert int(rest.split()[0]) - int(first.removeprefix("after:")) == 25 * 3600


def test_people_words_and_unread_narrow_the_list() -> None:
    """``from`` and ``to`` become quoted Gmail terms; words pass through; ``unread`` adds one."""
    ask = ask_of(
        {"query": "invoice", "from": "Sam Lee", "to": 'a"b@example.com', "unread": True},
        NOW, ZONE, searching=False,
    )
    assert ask.query == 'invoice from:"Sam Lee" to:"ab@example.com" -in:sent -in:drafts is:unread'
    assert ask.window == '"invoice", from Sam Lee, to ab@example.com, unread'
    assert ask.unread_only


def test_a_search_covers_all_his_mail_and_waiting_looks_back_fourteen_days() -> None:
    """A search does not leave sent mail out; ``waiting`` with no ``when`` is the last 14 days."""
    assert ask_of({"query": "wealthsimple"}, NOW, ZONE, searching=True).query == "wealthsimple"
    waiting = ask_of({"waiting": True}, NOW, ZONE, searching=False)
    start = int(datetime(2026, 10, 8, tzinfo=ZONE).timestamp())
    assert waiting.since == start
    assert waiting.waiting
    assert waiting.window == "the last 14 days"


def test_words_he_cannot_be_read_as_a_day_are_refused_with_the_forms_that_work() -> None:
    """``when`` goes through the one day resolver; a failure says what it reads."""
    with pytest.raises(ToolError) as caught:
        ask_of({"when": "whenever"}, NOW, ZONE, searching=False)
    assert caught.value.code == "invalid_arguments"
    assert "mail: I read" in str(caught.value)


# --- who wrote last ----------------------------------------------------------------------------


def _lasts(gmail: Gmail, query: str = "-in:sent") -> dict[str, Last]:
    box = Mailbox(gmail.tool)
    hits = box.search(query).hits
    heads = [one for one in box.heads([hit.id for hit in hits]) if one]
    return last_writers(box, heads, newest_by_thread(hits), NOW)


def test_each_conversation_says_whether_its_last_message_is_his_and_what_marks_the_others() -> None:
    """His reply ends one; the others say who wrote, bulk, automated or that he is only in Cc."""
    lasts = _lasts(_threads(_mailbox()))
    assert lasts["ta"].mine
    assert lasts["ta"].at == _ago(hours=4)
    waiting = lasts["tb"]
    assert (waiting.mine, waiting.waiting) == (False, True)
    assert waiting.by is not None
    assert waiting.by.shown == "Kim Ray"
    assert (lasts["tc"].bulk, lasts["tc"].waiting) == (True, False)
    assert (lasts["td"].automated, lasts["td"].waiting) == (True, False)
    assert (lasts["te"].to_him, lasts["te"].waiting) == (False, False)


def test_a_message_whose_headers_cannot_be_read_is_not_claimed_waiting() -> None:
    """Too big to read raw: nothing is said of its marks, and it does not count as waiting."""
    gmail = _mailbox()
    gmail.add("big1", "Big file", sender=SAM, to=ME, thread="tz", size=99_000_000)
    last = _lasts(gmail)["tz"]
    assert (last.bulk, last.automated, last.waiting) == (None, None, False)
    assert state_line(last, NOW, ZONE) == ""


def test_only_the_newest_message_of_a_conversation_is_asked_who_wrote_last() -> None:
    """An older message of a thread that is listed again newer has no verdict of its own."""
    gmail = _mailbox()
    gmail.add("o1", "Plan", sender=SAM, to=ME, thread="tp", at=_ago(days=3))
    gmail.add("o2", "Re: Plan", sender=SAM, to=ME, thread="tp", at=_ago(days=1))
    assert set(_lasts(gmail)) == {"tp"}


def test_his_own_message_in_a_list_is_his_last_word() -> None:
    """A note he sent himself, found by a search of all mail, ends its own conversation."""
    gmail = _mailbox()
    gmail.add("self1", "Reminder", sender=ME, to=ME, thread="ts", labels=SENT, at=_ago(hours=1))
    lasts = _lasts(gmail, "reminder")
    assert lasts["ts"].mine


def test_a_row_says_in_a_phrase_what_the_conversation_is() -> None:
    """The state line: his last word with its time, waiting, or what marks it."""
    assert state_line(Last(mine=True, at=_ago(hours=4)), NOW, ZONE) == (
        "you wrote last (today (Wed) 7:00 am)"
    )
    assert state_line(
        Last(mine=False, at=None, bulk=False, automated=False, to_him=True), NOW, ZONE,
    ) == "waiting on him"
    assert state_line(
        Last(mine=False, at=None, bulk=True, automated=True, to_him=False), NOW, ZONE,
    ) == "bulk mail, automated, he is not in To"


# --- the rows ----------------------------------------------------------------------------------


def _inbox(count: int) -> tuple[Gmail, Mailbox, Ask]:
    gmail = _mailbox()
    for n in range(count):
        gmail.add(
            f"m{n:02d}", f"Note {n}", sender=SAM, to=ME, at=_ago(minutes=n + 1),
            plain=f"Hi Allen,\nThis is note {n} and it has a few more words in it.",
        )
    ask = ask_of({}, NOW, ZONE, searching=False)
    return gmail, Mailbox(gmail.tool), ask


def test_a_page_is_eight_rows_numbered_across_the_whole_list() -> None:
    """Rows 9 to 11 come on page two, so a number means the same row on every page."""
    _, box, ask = _inbox(11)
    found = box.search(ask.query)
    first, gone = page_rows(box, found.hits, 0, ask, NOW)
    assert [row.n for row in first] == list(range(1, PAGE_ROWS + 1))
    assert gone == 0
    second, _ = page_rows(box, found.hits, PAGE_ROWS, ask, NOW)
    assert [row.n for row in second] == [9, 10, 11]
    assert second[0].head.id == "m08"


def test_a_row_carries_its_own_opening_line_not_gmails_snippet() -> None:
    """The opening is the cleaned text's first words; padding never reaches it."""
    gmail = _mailbox()
    padded = (
        f"<div style='display:none'>{PADDING}</div><p>Hi Allen,</p>"
        "<p>The quarterly report is attached for your review.</p>"
    )
    gmail.add("p1", "Report", sender=SAM, to=ME, html=padded, at=_ago(minutes=5))
    box = Mailbox(gmail.tool)
    ask = ask_of({}, NOW, ZONE, searching=False)
    (row,), _ = page_rows(box, box.search(ask.query).hits, 0, ask, NOW)
    assert row.opening == "Hi Allen, The quarterly report is attached for your review."
    shown = row_json(row, NOW, ZONE)
    assert shown["opening"] == row.opening
    assert PADDING[0] not in repr(shown)


def test_a_row_is_compact_and_every_field_is_bounded() -> None:
    """From, when, subject, opening, files; long values are cut, empty ones absent."""
    gmail = _mailbox()
    gmail.add(
        "r1", "S" * 300, sender=f'"{"N" * 90}" <n@example.com>', to=ME, at=_ago(minutes=3),
        plain="word " * 200, attachments=[(f"file{n}.csv", "text/csv", b"a,b") for n in range(5)],
    )
    box = Mailbox(gmail.tool)
    ask = ask_of({}, NOW, ZONE, searching=False)
    (row,), _ = page_rows(box, box.search(ask.query).hits, 0, ask, NOW)
    shown = row_json(row, NOW, ZONE)
    assert list(shown) == ["n", "message", "from", "when", "subject", "opening", "files", "state"]
    assert len(shown["from"]) == 40
    assert len(shown["subject"]) == 100
    assert len(shown["opening"]) == 140
    assert shown["files"] == ["file0.csv", "file1.csv", "file2.csv", "+2 more"]
    assert shown["when"] == "today (Wed) 10:57 am"
    assert len(repr(shown)) < 600


def test_a_window_lists_read_and_unread_and_marks_the_unread() -> None:
    """With ``when`` the rows are read or not, each saying which; the scope says no more."""
    gmail = _mailbox()
    gmail.add("u1", "Unread one", sender=SAM, to=ME, at=_ago(hours=1))
    gmail.add("r1", "Read one", sender=SAM, to=ME, at=_ago(hours=2), labels=("INBOX",))
    box = Mailbox(gmail.tool)
    ask = ask_of({"when": "today"}, NOW, ZONE, searching=False)
    rows, _ = page_rows(box, box.search(ask.query).hits, 0, ask, NOW)
    assert {row.head.id: row.unread for row in rows} == {"u1": True, "r1": False}
    assert row_json(rows[0], NOW, ZONE)["unread"] is True
    assert "unread" not in row_json(rows[1], NOW, ZONE)
    default = ask_of({}, NOW, ZONE, searching=False)
    unread_rows, _ = page_rows(box, box.search(default.query).hits, 0, default, NOW)
    assert [row.unread for row in unread_rows] == [None]


def test_a_message_gmail_no_longer_has_is_skipped_and_counted() -> None:
    """A row that vanished between the search and the read is not shown; the page says so."""
    gmail, box, ask = _inbox(3)
    hits = box.search(ask.query).hits
    del gmail.messages["m01"]
    rows, gone = page_rows(box, hits, 0, ask, NOW)
    assert [row.head.id for row in rows] == ["m00", "m02"]
    assert gone == 1


def test_a_row_without_a_body_has_no_opening() -> None:
    """A mail that is only a picture says nothing of itself; Gmail's snippet is not used."""
    gmail = _mailbox()
    gmail.add("img1", "Photo", sender=SAM, to=ME, html="<img src='x.png' width='1' height='1'>")
    box = Mailbox(gmail.tool)
    assert opening(box.head("img1", full=True)) == ""


# --- what is not shown -------------------------------------------------------------------------


def test_what_is_not_shown_is_counted_by_gmails_categories() -> None:
    """One ids-only search per category; a message is in the one whose matches hold it."""
    gmail = _mailbox()
    for n in range(4):
        gmail.add(f"p{n}", f"Person {n}", sender=SAM, to=ME, at=_ago(minutes=n), labels=PRIMARY)
    for n in range(3):
        gmail.add(f"s{n}", f"Sale {n}", sender=NEWS, to=ME, at=_ago(hours=1, minutes=n),
                  labels=PROMOTIONS)
    gmail.add("u0", "Receipt", sender=NEWS, to=ME, at=_ago(hours=2), labels=UPDATES)
    gmail.add("x0", "Odd", sender=NEWS, to=ME, at=_ago(hours=3), labels=("INBOX", "UNREAD"))
    box = Mailbox(gmail.tool)
    hits = box.search("in:inbox is:unread").hits
    assert not_shown(box, "in:inbox is:unread", hits[2:]) == {
        "Primary": 2, "Promotions": 3, "Updates": 1, "other": 1,
    }
    assert not_shown(box, "in:inbox is:unread", []) == {}
    category_searches = [
        args["query"] for args in gmail.of("gmail_search") if "category:" in args["query"]
    ]
    assert len(category_searches) == 5
    assert all(args["maxResults"] <= 100 for args in gmail.of("gmail_search"))


# --- waiting on him ----------------------------------------------------------------------------


def test_waiting_lists_only_threads_someone_else_wrote_last_to_him_that_are_not_bulk() -> None:
    """The newsletter, the alert, the Cc and the thread he answered are left out; one waits."""
    gmail = _threads(_mailbox())
    box = Mailbox(gmail.tool)
    ask = ask_of({"waiting": True}, NOW, ZONE, searching=False)
    found = find_waiting(box, ask, NOW)
    assert [hit.id for hit in found.hits] == ["b1"]
    assert (found.threads, found.answered, found.skipped, found.unreadable) == (5, 1, 0, 0)
    assert not found.more
    assert waiting_line(found, NOW) == (
        "1 thread where someone else wrote last and is waiting on him: Kim Ray (2 days ago)"
    )


def test_waiting_checks_the_newest_threads_and_says_how_many_it_left() -> None:
    """Past the budget the oldest threads are not read; the count of them is stated."""
    gmail = _mailbox()
    count = WAITING_CHECKED + 5
    for n in range(count):
        gmail.add(f"w{n:02d}", f"Ask {n}", sender=SAM, to=ME, thread=f"t{n:02d}",
                  at=_ago(minutes=n + 1))
    box = Mailbox(gmail.tool)
    found = find_waiting(box, ask_of({"waiting": True}, NOW, ZONE, searching=False), NOW)
    assert (found.threads, len(found.hits), found.skipped) == (count, WAITING_CHECKED, 5)
    assert found.hits[0].id == "w00"
    raw_reads = [a for a in gmail.of("gmail_get") if a["format"] == "raw"]
    assert len(raw_reads) == WAITING_CHECKED


def test_a_thread_whose_headers_cannot_be_read_is_counted_not_claimed() -> None:
    """Over the size limit: not waiting, not answered, and the count says so."""
    gmail = _mailbox()
    gmail.add("big1", "Huge", sender=SAM, to=ME, thread="tz", size=99_000_000, at=_ago(hours=1))
    box = Mailbox(gmail.tool, raw_max_bytes=1_000_000)
    found = find_waiting(box, ask_of({"waiting": True}, NOW, ZONE, searching=False), NOW)
    assert (len(found.hits), found.unreadable) == (0, 1)


def test_his_sent_mail_is_one_search_from_the_windows_start() -> None:
    """The sent search carries the window's opening; it is not one search per thread."""
    gmail = _threads(_mailbox())
    box = Mailbox(gmail.tool)
    ask = ask_of({"waiting": True}, NOW, ZONE, searching=False)
    find_waiting(box, ask, NOW)
    sent = [a["query"] for a in gmail.of("gmail_search") if a["query"].startswith("in:sent")]
    assert sent.count(f"in:sent after:{ask.since}") == 1
    assert stamp(NOW)  # the fake's date format is the header's
