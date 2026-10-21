"""ADR 0295: the nightly letter on his Claude chats is kept raw and read into the archive.

Invented letters only. The parser is checked on a good, a partial and a garbage letter; the real
poller runs over a fake ``gmail`` client holding a draft, a sent copy, an edited draft and a mail
that is not a daily letter; the readers (the day line and the ``archive`` tool) and the shipped
config are asserted on what they return. No model, no network.
"""

# ruff: noqa: RUF001, E501, PT018 - the letters use an en dash and Chinese; asserts compare tuples
from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest
import yaml

from jarvis.execution.archive_tool import build_archive_tool
from jarvis.execution.tools import Tool, ToolError
from jarvis.runtime.bootstrap.intakes import _claude_daily
from jarvis.runtime.claude_daily import ClaudeDailyIntake, ClaudeDailySettings
from jarvis.state import imports
from jarvis.state.day_line import DaySources, day_window, fold_day
from jarvis.state.event_log import open_event_log
from jarvis.state.import_parsers import claude_daily, is_daily_subject
from jarvis.state.memory_db import open_memory_db
from tests.canary._helpers import repo_root
from tests.support.clock import FakeClock
from tests.support.runtime_config import runtime_config
from tests.support.tool_context import tool_context

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

VANCOUVER = ZoneInfo("America/Vancouver")
SUBJECT = "Jarvis Claude 日报 2026-10-18"
LETTER = """jarvis-claude-daily: 1
date: 2026-10-18
timezone: America/Vancouver
chats: 3

## Bus pass question
- id: https://claude.ai/chat/aaa111
- project: -
- time: 09:12–10:40
- messages: 14
- summary: Asked how a monthly bus pass works for a student.
  Ended with the price of the pass.
- decisions: -
- todos: buy the pass on Monday
- mentions: BC Transit; Victoria
- mood: curious

## Resume bullets
- id: bbb222
- project: Co-op
- time: 13:00-13:30
- messages: 6
- summary: Rewrote three resume bullets.
- decisions: keep one page
- todos: -
- mentions: -

## Pottery plan
- project: -
- time: 23:30-01:15
- messages: 4
- summary: Planned the Tuesday class.
- decisions: -
- todos: -
- mentions: Studio 4
"""


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _parse(text: str, name: str = SUBJECT, at: datetime | None = None) -> list[Any]:
    fallback = _ms(at or datetime(2026, 10, 19, 8, 0, tzinfo=UTC))
    return list(claude_daily(name, text.encode(), fallback))


# --- the parser ----------------------------------------------------------------------------


def test_a_good_letter_is_a_day_entry_and_one_entry_per_chat() -> None:
    """Keys carry the day, the chat's id (else a title digest) and a hash; times are in his zone."""
    day, bus, resume, pottery = _parse(LETTER)

    assert (day.source, day.kind, day.zone, day.pos) == ("claude_daily", "daily", "America/Vancouver", "day")
    assert day.key.startswith("day:2026-10-18:")
    assert day.at_ms == _ms(datetime(2026, 10, 18, 12, 0, tzinfo=VANCOUVER))
    assert (day.fields["parsed"], day.fields["sections"], day.fields["chats"]) == (True, 3, "3")
    assert day.fields["titles"] == ["Bus pass question", "Resume bullets", "Pottery plan"]
    assert day.fields["start_ms"] == _ms(datetime(2026, 10, 18, 9, 12, tzinfo=VANCOUVER))
    assert day.fields["end_ms"] == _ms(datetime(2026, 10, 19, 1, 15, tzinfo=VANCOUVER))
    assert day.text == "\n".join(line for line in LETTER.splitlines() if line)  # no blank lines

    assert bus.kind == "daily_chat"
    assert bus.key.startswith("chat:2026-10-18:https://claude.ai/chat/aaa111:")
    assert resume.key.startswith("chat:2026-10-18:bbb222:")
    assert pottery.key.startswith("chat:2026-10-18:title-")  # no id: a digest of the title
    assert bus.at_ms == _ms(datetime(2026, 10, 18, 9, 12, tzinfo=VANCOUVER))  # en dash read
    assert bus.fields["end_ms"] == _ms(datetime(2026, 10, 18, 10, 40, tzinfo=VANCOUVER))
    assert pottery.fields["end_ms"] - pottery.at_ms == 105 * 60 * 1000  # past midnight
    assert bus.fields["summary"] == "Asked how a monthly bus pass works for a student.\nEnded with the price of the pass."
    assert (bus.fields["todos"], bus.fields["messages"], bus.fields["project"]) == ("buy the pass on Monday", "14", "-")
    assert bus.fields["mood"] == "curious"  # an unknown line is kept as it is
    assert "price of the pass" in bus.text
    assert len({one.key for one in (day, bus, resume, pottery)}) == 4


def test_a_revised_chat_is_a_new_entry_and_the_unchanged_ones_keep_their_keys() -> None:
    """A changed summary changes that chat's key and the day's; other line breaks change nothing."""
    first = _parse(LETTER)
    again = _parse(LETTER.replace("\n- ", "\n\n- "))
    revised = _parse(LETTER.replace("Rewrote three resume bullets.", "Rewrote four resume bullets."))

    assert [one.key for one in again] == [one.key for one in first]
    assert [a.key == b.key for a, b in zip(first, revised, strict=True)] == [False, True, False, True]


def test_a_partial_letter_is_read_with_what_it_has() -> None:
    """No header, no timezone, a dash for the time, no id, a missing field: still read."""
    day, chat = _parse(
        "## Quick question\n- time: 8:05 - 8:20\n- summary: asked about a recipe\n",
        name="Jarvis Claude 日报 2026-10-17",
    )
    assert (day.zone, day.fields["date"], day.fields["parsed"]) == ("UTC", "2026-10-17", True)
    assert chat.at_ms == _ms(datetime(2026, 10, 17, 8, 5, tzinfo=UTC))
    assert chat.fields["end_ms"] == _ms(datetime(2026, 10, 17, 8, 20, tzinfo=UTC))
    assert "messages" not in chat.fields

    # a header alone: the day, no chats; a time that is no time is the day's noon
    (only,) = _parse("jarvis-claude-daily: 1\ndate: 2026-10-16\ntimezone: Mars/Olympus\nchats: 0\n")
    assert (only.zone, only.fields["sections"], only.fields["parsed"]) == ("Mars/Olympus", 0, False)
    assert only.at_ms == _ms(datetime(2026, 10, 16, 12, 0, tzinfo=UTC))  # an unknown zone reads as UTC
    _day, odd = _parse("date: 2026-10-16\n\n## Odd\n- time: 25:99\n- summary: x\n")
    assert odd.at_ms == _ms(datetime(2026, 10, 16, 12, 0, tzinfo=UTC))


@pytest.mark.parametrize("text", [
    "I could not find any chats today, sorry.",
    "\x00\x01\x02 binary \\ufffd",
    "date: 2026-13-45\n- :\n-\n",
    "",
])
def test_a_letter_that_does_not_parse_is_still_one_whole_day_entry(text: str) -> None:
    """Never dropped: one day entry with the whole text, dated by its date, else by arrival."""
    (day,) = _parse(text, name="no date here")
    assert (day.kind, day.text) == ("daily", text.strip())
    assert (day.fields["parsed"], day.fields["sections"]) == (False, 0)
    assert day.fields["date"] == "2026-10-19"  # an impossible date is none: when it came, in UTC


def test_the_subject_is_checked_by_its_start() -> None:
    """Only a subject that starts with the title is a daily letter's; a reply or a lookalike is not."""
    assert is_daily_subject(SUBJECT) and is_daily_subject("  jarvis claude 日报 2026-10-18 ")
    assert not is_daily_subject("Re: Jarvis Claude 日报 2026-10-18")
    assert not is_daily_subject("Jarvis Claude newsletter")


# --- the poller ----------------------------------------------------------------------------


def _mail(key: str, subject: str, body: str, labels: list[str], hour: int) -> dict[str, Any]:
    stamp = datetime(2026, 10, 19, hour, 0, tzinfo=UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    return {
        "id": key, "threadId": key, "labelIds": labels, "from": "Allen <me@example.com>",
        "subject": subject, "date": stamp, "body": body,
    }


class _Gmail:
    """Answers like the gmail server: newest first, two to a page, drafts and sent included."""

    def __init__(self, mails: list[dict[str, Any]]) -> None:
        self.mails = mails
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert server == "gmail"
        self.calls.append((tool, dict(args)))
        assert tool in {"gmail_search", "gmail_get"}, f"the poller may only read: {tool}"
        if tool == "gmail_search":
            listed = sorted(
                (m for m in self.mails if "Jarvis Claude" in m["subject"]),
                key=lambda m: m["id"], reverse=True,
            )
            first = int(args.get("pageToken") or 0)
            reply: dict[str, Any] = {"messages": [{"id": m["id"]} for m in listed[first : first + 2]]}
            if first + 2 < len(listed):
                reply["nextPageToken"] = str(first + 2)
            return {"text": json.dumps(reply)}
        mail = next(m for m in self.mails if m["id"] == args["messageId"])
        body = mail if args["format"] == "full" else {k: v for k, v in mail.items() if k != "body"}
        return {"text": json.dumps(body)}

    def fulls(self) -> list[str]:
        return [a["messageId"] for t, a in self.calls if t == "gmail_get" and a["format"] == "full"]


class _Connections:
    def __init__(self, gmail: _Gmail) -> None:
        self.gmail = gmail

    def client_for(self, server: str) -> _Gmail:
        assert server == "gmail"
        return self.gmail


REVISED = LETTER.replace("Rewrote three resume bullets.", "Rewrote four resume bullets.")
WRAPPED = "<div>" + LETTER.replace("\n", "<br>\n") + "</div>"
MAILS = [
    _mail("d1", SUBJECT, LETTER, ["DRAFT"], 3),  # the first draft
    _mail("s1", SUBJECT, WRAPPED, ["SENT", "INBOX"], 4),  # the same letter sent to himself, as HTML
    _mail("d2", SUBJECT, REVISED, ["DRAFT"], 5),  # a re-run: an edited draft
    _mail("x1", "Jarvis Claude newsletter", "## Not a daily letter\n- summary: no", ["INBOX"], 6),
]


class _Rig:
    def __init__(self, tmp_path: Path, mails: list[dict[str, Any]] = MAILS) -> None:
        self.root = tmp_path
        self.db = tmp_path / "memory.db"
        open_memory_db(self.db).close()
        self.gmail = _Gmail([dict(m) for m in mails])
        settings = ClaudeDailySettings(3600, date(2026, 10, 18), 30)
        self.intake = ClaudeDailyIntake(
            settings, _Connections(self.gmail), self.db, tmp_path / imports.IMPORTS_DIRNAME,  # type: ignore[arg-type]
        )

    def entries(self, kind: str = "") -> list[imports.Entry]:
        with closing(open_memory_db(self.db)) as conn:
            found = imports.entries(conn, source="claude_daily", kinds=(kind,) if kind else ())
        return sorted(found, key=lambda one: (one.at_ms, one.pos, one.text))

    def raw(self) -> list[str]:
        return sorted(p.name.partition("-")[2] for p in (self.root / "imports" / "claude_daily").glob("*.json"))


def test_a_draft_a_sent_copy_and_an_edited_draft_are_kept_without_duplicates(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Three letters of one day: the sent copy adds nothing, the edit adds only what changed."""
    rig = _Rig(tmp_path)
    with caplog.at_level("WARNING"):
        assert rig.intake.poll_once() == 3
    assert "differs" not in caplog.text  # the HTML copy reads exactly like the draft

    # every letter raw, by its message id; the other mail is not kept
    assert rig.raw() == ["d1.json", "d2.json", "s1.json"]
    kept = json.loads(next((tmp_path / "imports" / "claude_daily").glob("*-d1.json")).read_text(encoding="utf-8"))
    assert kept["message_id"] == "d1" and kept["full"]["labelIds"] == ["DRAFT"]
    assert kept["metadata"]["subject"] == SUBJECT and "body" not in kept["metadata"]

    days = rig.entries("daily")
    assert len(days) == 2  # the draft and its HTML sent copy are one revision; the edit is another
    assert {d.fields["date"] for d in days} == {"2026-10-18"}
    chats = rig.entries("daily_chat")
    assert len(chats) == 4  # three chats, plus the one chat the edit changed
    assert sorted(c.fields["title"] for c in chats) == [
        "Bus pass question", "Pottery plan", "Resume bullets", "Resume bullets",
    ]
    assert sorted(c.fields["summary"] for c in chats if c.fields["title"] == "Resume bullets") == [
        "Rewrote four resume bullets.", "Rewrote three resume bullets.",
    ]

    # what asked Gmail: reads only, the net query, then the drafts, each paged to the end
    searches = [a for t, a in rig.gmail.calls if t == "gmail_search"]
    net = 'subject:"Jarvis Claude" after:2026/10/18'
    assert [a["query"] for a in searches] == [net] * 2 + [f"{net} in:drafts"] * 2
    assert all("-in:" not in a["query"] for a in searches)
    assert sorted(rig.gmail.fulls()) == ["d1", "d2", "s1"]  # the other mail's body was never read


def test_a_second_cycle_changes_nothing_and_reads_no_body(tmp_path: Path) -> None:
    """Idempotent: nothing new is added and no body is fetched again; a later letter is kept."""
    rig = _Rig(tmp_path)
    rig.intake.poll_once()
    before = rig.entries()
    reads = len(rig.gmail.fulls())

    assert rig.intake.poll_once() == 0
    assert rig.entries() == before
    assert len(rig.gmail.fulls()) == reads
    # a letter that came since is kept at the next cycle
    rig.gmail.mails.append(_mail("d3", SUBJECT, LETTER + "\n- extra: later\n", ["DRAFT"], 7))
    assert rig.intake.poll_once() == 1
    assert "d3.json" in rig.raw()


def test_a_letter_that_does_not_parse_is_kept_raw_and_read_as_one_entry(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure is logged by id and size, never by content; one unreadable letter does not stop others."""
    odd = _mail("g1", SUBJECT, "Sorry, nothing to summarise: SECRET-WORDS", ["DRAFT"], 3)
    broken = _mail("g2", SUBJECT, LETTER, ["DRAFT"], 4)
    rig = _Rig(tmp_path, [odd, broken])
    real = rig.gmail.call

    def flaky(server: str, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if tool == "gmail_get" and args["messageId"] == "g2" and args["format"] == "full":
            msg = "hiccup"
            raise ToolError(msg, code="mcp_tool_error")
        return real(server, tool, args)

    rig.gmail.call = flaky  # type: ignore[method-assign]
    with caplog.at_level("INFO"):
        assert rig.intake.poll_once() == 1
    (day,) = rig.entries()
    assert day.kind == "daily" and "SECRET-WORDS" in day.text and day.fields["parsed"] is False
    assert day.fields["date"] == "2026-10-18"  # from the subject
    assert rig.raw() == ["g1.json"]
    assert "SECRET-WORDS" not in caplog.text and "nothing to summarise" not in caplog.text
    assert "no chat sections" in caplog.text and "cannot read g2 (ToolError)" in caplog.text

    rig.gmail.call = real  # type: ignore[method-assign]
    assert rig.intake.poll_once() == 1  # g2 is tried again, g1 is not
    assert rig.raw() == ["g1.json", "g2.json"]


def test_a_letter_whose_entries_were_read_but_raw_was_not_kept_is_kept_next_time(tmp_path: Path) -> None:
    """Entries commit before the raw file: a crash between the two is mended, not duplicated."""
    rig = _Rig(tmp_path, MAILS[:1])
    rig.intake.poll_once()
    (raw,) = (tmp_path / "imports" / "claude_daily").glob("*.json")
    raw.unlink()
    before = rig.entries()

    assert rig.intake.poll_once() == 1
    assert rig.raw() == ["d1.json"] and rig.entries() == before


def test_with_gmail_not_connected_nothing_is_read(tmp_path: Path) -> None:
    """A poller with no gmail server reads nothing and does not fail."""
    rig = _Rig(tmp_path)

    class _Off:
        def client_for(self, _server: str) -> None:
            msg = "not connected"
            raise ToolError(msg, code="mcp_unavailable")

    rig.intake._connections = _Off()  # type: ignore[assignment]  # noqa: SLF001
    assert rig.intake.poll_once() == 0


# --- the readers ---------------------------------------------------------------------------


def test_the_import_command_reads_a_letter_file_the_same_way(tmp_path: Path) -> None:
    """`jarvis import claude_daily FILE` reads a letter file by the same parser."""
    letter = tmp_path / "day.md"
    letter.write_text(LETTER, encoding="utf-8")
    with closing(open_memory_db(tmp_path / "memory.db")) as conn:
        first = imports.import_files(conn, tmp_path / "imports", "claude_daily", [letter])
        second = imports.import_files(conn, tmp_path / "imports", "claude_daily", [letter])
    assert (first.added, second.added, second.held) == (4, 0, 4)  # again: all held


def test_the_day_line_shows_the_newest_letter_of_a_day_beside_that_day(tmp_path: Path) -> None:
    """One item a day, from the letter added last, on its own day only."""
    rig = _Rig(tmp_path)
    rig.intake.poll_once()
    conn = open_event_log(tmp_path / "events.db")
    now = _ms(datetime(2026, 10, 19, 20, 0, tzinfo=UTC))

    def items(day: str) -> list[dict[str, Any]]:
        first, last = day_window(date.fromisoformat(day), VANCOUVER)
        line = fold_day(conn, first, last, now, DaySources(VANCOUVER, memory_db=rig.db))
        return [i for i in line.items if i["kind"] == "claude_day"]

    (item,) = items("2026-10-18")
    assert item["id"] == "claude-day-2026-10-18" and item["lane"] == "you"
    assert item["text"] == "Bus pass question; Resume bullets; Pottery plan"
    assert (item["chats"], item["state"], item["source"]) == (3, "done", "claude_daily")
    assert item["start_ms"] == _ms(datetime(2026, 10, 18, 9, 12, tzinfo=VANCOUVER))
    assert items("2026-10-17") == [] and items("2026-10-19") == []
    with closing(open_memory_db(rig.db)) as db:
        first, last = day_window(date(2026, 10, 18), VANCOUVER)
        (newest,) = imports.latest_daily(db, first, last)
    assert "Rewrote four resume bullets." in newest.text  # the edit stands for the day
    conn.close()


def test_the_archive_finds_the_letters_by_source_day_and_words(tmp_path: Path) -> None:
    """The archive tool finds, filters and opens the letters' entries like any other source."""
    rig = _Rig(tmp_path)
    rig.intake.poll_once()
    tool: Tool = build_archive_tool(rig.db)[0]

    def call(**args: object) -> dict[str, Any]:
        return cast("dict[str, Any]", tool.handler(args, tool_context(clock=FakeClock(datetime(2026, 10, 20, 9, 0, tzinfo=UTC), zone=VANCOUVER))))

    found = call(source="claude_daily", query=["pottery"])
    assert found["entries"] and all(e["source"] == "claude_daily" for e in found["entries"])
    assert any("Planned the Tuesday class" in e["text"] for e in found["entries"])
    assert call(source="claude_daily", date="2026-10-17")["entries"] == []
    opened = call(open=found["entries"][0]["id"])
    assert opened["entry"]["fields"]["date"] == "2026-10-18"
    assert call(source="claude")["entries"] == []  # the export's source is not mixed in


# --- config --------------------------------------------------------------------------------


def test_off_by_default_and_the_block_reads_when_enabled(tmp_path: Path) -> None:
    """Shipped off, Gmail letters off but the Mac's report still read; bad values: see the load."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    block = shipped["claude_daily"]
    assert block["enabled"] is False
    assert {"poll_s": 3600, "backfill_since": date(2026, 10, 18), "max_letters_per_cycle": 30}.items() <= block.items()
    connections = _Connections(_Gmail([]))
    args: Any = (connections, tmp_path / "memory.db", tmp_path)

    def build(raw: Mapping[str, Any]) -> ClaudeDailyIntake:
        return _claude_daily(runtime_config(raw).claude_daily, *args)

    # off: no Gmail polling, but the Mac's code report is still read (ADR 0303)
    offs: list[Mapping[str, Any]] = [shipped, {}]
    for off in offs:
        quiet = build(off)
        assert isinstance(quiet, ClaudeDailyIntake) and quiet._settings.gmail is False  # noqa: SLF001
    built = build({"claude_daily": {**block, "enabled": True}})
    assert isinstance(built, ClaudeDailyIntake)
    assert built._settings.gmail is True  # noqa: SLF001
