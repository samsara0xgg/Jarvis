"""ADR 0317, 0362: an exam recording's ``mailbox`` is invented mail a fake Gmail server serves.

The messages a spec describes are what a search finds and a read returns, whatever the tool asks
and in whatever order; a spec's ``repeat`` makes numbered copies, each older than the one before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from scripts.tool_exam_mailbox import ME, build
from scripts.tool_exam_tools import gmail_failures

if TYPE_CHECKING:
    from scripts.tool_exam_tools import Box

NOW = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)


def _spec() -> dict[str, Any]:
    return {"messages": [
        {
            "id": "digest{i}", "subject": "Digest {i}", "sender": "Digest <digest@example.com>",
            "ago": 20, "every": 5, "repeat": 3,
            "headers": {"List-Unsubscribe": "<mailto:stop@example.com>"},
        },
        {
            "id": "statement", "subject": "Statement", "ago": 90, "to": ME,
            "plain": ["Your statement is attached.", {"repeat": 2, "text": "Please pay."}],
            "attachments": [
                {"name": "statement.pdf", "type": "application/pdf", "pdf": [["Total due $12.40"]]},
                {"name": "scan.pdf", "type": "application/pdf", "pdf": [[]]},
                {"name": "notes.txt", "type": "text/plain", "text": "Pay by Friday."},
            ],
        },
        {
            "id": "old", "subject": "Last year", "at": "2025-10-21T09:00:00+00:00",
            "labels": ["INBOX"],
        },
    ]}


def test_a_search_finds_the_messages_the_spec_describes_newest_first() -> None:
    """Copies are numbered and each ``every`` minutes older; ``at`` is a fixed time."""
    gmail = build(_spec(), NOW)
    assert gmail.ids("in:inbox is:unread") == ["digest1", "digest2", "digest3", "statement"]
    assert gmail.ids("in:inbox") == [
        "digest1", "digest2", "digest3", "statement", "old",
    ]
    assert gmail.ids("subject:Digest") == ["digest1", "digest2", "digest3"]
    assert gmail.ids("subject:Statement") == ["statement"]


def test_a_read_returns_the_text_the_files_and_the_headers_the_spec_gives() -> None:
    """A repeated paragraph is repeated; a PDF is a file, and a page with no lines is a scan."""
    gmail = build(_spec(), NOW)
    full = gmail.tool("gmail_get", {"messageId": "statement", "format": "full"})
    assert full["body"] == "Your statement is attached.\n\nPlease pay.\n\nPlease pay.\n"
    assert [(one["filename"], one["mimeType"]) for one in full["attachments"]] == [
        ("statement.pdf", "application/pdf"),
        ("scan.pdf", "application/pdf"),
        ("notes.txt", "text/plain"),
    ]
    assert full["to"] == ME
    assert gmail.messages["digest2"].message()["List-Unsubscribe"] == "<mailto:stop@example.com>"
    assert gmail.labels("old") == {"INBOX"}


def test_expect_gmail_counts_the_calls_made_to_the_mailbox_that_hold_the_arguments() -> None:
    """``expect.gmail``: calls of a tool with the arguments and query given; ``count`` is exact."""
    gmail = build(_spec(), NOW)
    gmail.tool("gmail_search", {"query": "in:inbox is:unread", "maxResults": 10})
    gmail.tool("gmail_batchModify", {"messageIds": ["old"], "removeLabelIds": ["INBOX"]})
    box = cast("Box", SimpleNamespace(gmail=gmail))
    archived = {"messageIds": ["old"], "removeLabelIds": ["INBOX"]}
    assert gmail_failures(box, [
        {"tool": "gmail_batchModify", "args": archived, "count": 1},
        {"tool": "gmail_search", "query": "^in:inbox"},
        {"tool": "gmail_send", "count": 0},
    ]) == []
    wrong = gmail_failures(box, [
        {"tool": "gmail_batchModify", "args": {"messageIds": ["digest1"]}},
        {"tool": "gmail_search", "query": "^from:", "count": 1},
        {"tool": "gmail_batchModify", "count": 0},
    ])
    assert len(wrong) == 3
    assert wrong[0].endswith("expected some call(s), got 0")
    assert wrong[1].endswith("expected 1 call(s), got 0")
    assert wrong[2].endswith("expected 0 call(s), got 1")


def test_expect_gmail_needs_a_recording_with_a_mailbox() -> None:
    """An item that asks about Gmail calls without a mailbox fails, rather than passing empty."""
    box = cast("Box", SimpleNamespace(gmail=None))
    assert gmail_failures(box, [{"tool": "gmail_send", "count": 0}]) == [
        "expect.gmail needs a recording with a mailbox",
    ]
    assert gmail_failures(box, []) == []
