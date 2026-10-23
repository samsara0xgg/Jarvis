"""ADR 0317, 0362: an exam recording's ``mailbox`` is invented mail a fake Gmail server serves.

The messages a spec describes are what a search finds and a read returns, whatever the tool asks
and in whatever order; a spec's ``repeat`` makes numbered copies, each older than the one before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from scripts.tool_exam_mailbox import ME, build

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
