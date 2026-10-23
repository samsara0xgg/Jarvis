"""ADR 0317, 0362: the mail checkers derive the right answer from the recording's own mailbox.

Each takes the item, its recording and a decoded result; a result that is right gets no complaint
and one that miscounts, misorders or leaves out a thread gets a named one.
"""

from __future__ import annotations

from typing import Any

from scripts.tool_exam_checkers import mail_unread_counted_by_category, mail_waiting_threads
from scripts.tool_exam_tools import Recording

ITEM = {"at": "2026-10-21T11:00:00-07:00", "zone": "America/Vancouver"}
PROMO = ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"]
UPDATES = ["INBOX", "UNREAD", "CATEGORY_UPDATES"]
NEWS = {"List-Unsubscribe": "<mailto:stop@example.com>"}


def _recording(messages: list[dict[str, Any]]) -> Recording:
    return Recording({"mailbox": {"messages": messages}})


def _unread() -> Recording:
    return _recording([
        {"id": "p{i}", "subject": "Plan {i}", "ago": 5, "every": 10, "repeat": 3},
        {"id": "q{i}", "subject": "Deal {i}", "ago": 60, "every": 5, "repeat": 4, "labels": PROMO},
        {"id": "u{i}", "subject": "Notice {i}", "ago": 120, "every": 5, "repeat": 2,
         "labels": UPDATES},
        {"id": "read1", "subject": "Seen", "ago": 1, "labels": ["INBOX", "CATEGORY_PERSONAL"]},
    ])


def _rows(*ids: str) -> list[dict[str, str]]:
    return [{"message": one} for one in ids]


SUMMARY = (
    "9 unread in his inbox, newest first. Showing 1-5. Not shown: 4 (Promotions 2, Updates 2);"
    " page=2 gives the next 8."
)
GOOD = {"summary": SUMMARY, "rows": _rows("p1", "p2", "p3", "q1", "q2")}


def test_the_unread_count_and_the_categories_left_out_are_derived_from_the_mailbox() -> None:
    """Nine unread (the read one does not count); the rows are the five newest."""
    rec = _unread()
    assert mail_unread_counted_by_category(ITEM, rec, GOOD) == []
    wrong_total = {**GOOD, "summary": SUMMARY.replace("9 unread", "10 unread")}
    assert "holds 9 unread" in mail_unread_counted_by_category(ITEM, rec, wrong_total)[0]
    no_category = {**GOOD, "summary": SUMMARY.replace("Updates 2", "Updates 1")}
    assert "lacks 'Updates 2'" in mail_unread_counted_by_category(ITEM, rec, no_category)[0]
    not_newest = {**GOOD, "rows": _rows("p1", "p2", "p3", "q2", "q1")}
    assert mail_unread_counted_by_category(ITEM, rec, not_newest) == [
        "the rows are not the newest unread mails, newest first",
    ]
    assert mail_unread_counted_by_category(ITEM, Recording(), GOOD) == [
        "the recording holds no mailbox",
    ]


def _threads() -> Recording:
    me = "Allen <allen@example.com>"
    return _recording([
        {"id": "w1", "subject": "Review", "sender": "Sam Lee <sam@example.com>", "thread": "W1",
         "ago": 3000},
        {"id": "n1", "subject": "Digest", "thread": "N1", "ago": 300, "headers": NEWS},
        {"id": "a1", "subject": "Dinner", "thread": "A1", "ago": 6000},
        {"id": "a2", "subject": "Re: Dinner", "sender": me, "to": "Sam Lee <sam@example.com>",
         "thread": "A1", "ago": 5000, "labels": ["SENT"]},
        {"id": "c1", "subject": "Lunch", "thread": "C1", "ago": 100,
         "to": "Team <team@example.com>"},
        {"id": "o1", "subject": "Ancient", "thread": "O1", "ago": 60 * 24 * 30},
    ])


def test_waiting_threads_are_those_last_written_by_someone_else_to_him_and_not_bulk() -> None:
    """Of six threads only W1 waits: N1 is bulk, A1 ends with him, C1 is not to him, O1 is old."""
    rec = _threads()
    assert mail_waiting_threads(ITEM, rec, {"rows": _rows("w1")}) == []
    assert mail_waiting_threads(ITEM, rec, {"rows": []}) == [
        "thread W1 waits on him; the result leaves it out",
    ]
    extra = mail_waiting_threads(ITEM, rec, {"rows": _rows("w1", "n1", "c1")})
    assert extra == [
        "thread C1 does not wait on him; the result lists it",
        "thread N1 does not wait on him; the result lists it",
    ]
