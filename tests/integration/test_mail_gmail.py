"""ADR 0362 to 0364: the Gmail layer pages, counts, reads raw and says what it could not.

A fake of the Workspace server (``tests.support.gmail_fake``) answers; each check asserts what the
layer returned or what it asked Gmail for. No network, no key.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.tools import ToolError
from tests.support.gmail_fake import PRIMARY, SENT, Gmail
from tests.support.mail_raw import ALEX, pdf

if TYPE_CHECKING:
    from collections.abc import Mapping


def _inbox(count: int) -> Gmail:
    gmail = Gmail()
    for n in range(count):
        gmail.add(f"m{n:03d}", f"Note {n}", at=gmail.now - timedelta(minutes=n + 1))
    return gmail


def test_a_search_pages_ids_only_to_the_end_and_is_exact() -> None:
    """230 matches are three searches of ids, newest first, and nothing is read."""
    gmail = _inbox(230)
    found = Mailbox(gmail.tool).search("in:inbox")
    assert (len(found.hits), found.more) == (230, False)
    assert found.hits[0].id == "m000"
    assert found.hits[-1].id == "m229"
    assert gmail.names() == ["gmail_search"] * 3
    assert [args["maxResults"] for args in gmail.of("gmail_search")] == [100, 100, 100]
    assert gmail.of("gmail_search")[1]["pageToken"] == "100"


def test_a_search_stops_at_the_cap_and_says_there_is_more() -> None:
    """With more than the cap it keeps the cap's worth and says so; the exact cap is exact."""
    gmail = _inbox(120)
    found = Mailbox(gmail.tool, cap=100).search("in:inbox")
    assert (len(found.hits), found.more) == (100, True)
    assert [args["maxResults"] for args in gmail.of("gmail_search")] == [100, 1]
    exact = _inbox(100)
    found = Mailbox(exact.tool, cap=100).search("in:inbox")
    assert (len(found.hits), found.more) == (100, False)


def test_a_search_of_nothing_is_an_empty_exact_list() -> None:
    """Gmail answers with no ``messages`` key at all; that is zero, not a failure."""
    found = Mailbox(Gmail().tool).search("in:inbox")
    assert (found.hits, found.more) == ((), False)


def test_a_failure_is_worded_by_what_it_was() -> None:
    """Not connected, a sign-in that needs renewing and a server that did not answer differ."""
    def not_connected(_tool: str, _args: Mapping[str, Any]) -> dict[str, Any]:
        msg = "mcp server 'gmail' is not connected"
        raise LookupError(msg)

    with pytest.raises(ToolError) as caught:
        Mailbox(not_connected).search("x")
    assert (caught.value.code, caught.value.kind) == ("unavailable", "impossible")
    assert "not connected" in str(caught.value)

    signed_out = Gmail(error="invalid_grant: token has expired")
    with pytest.raises(ToolError) as caught:
        Mailbox(signed_out.tool).search("x")
    assert caught.value.code == "unavailable"
    assert "needs reconnecting" in str(caught.value)

    down = Gmail(error="upstream connect error")
    with pytest.raises(ToolError) as caught:
        Mailbox(down.tool).search("x")
    assert caught.value.code == "network_error"
    assert "did not answer" in str(caught.value)


def test_a_failure_of_a_change_says_the_mail_could_not_be_changed() -> None:
    """A read that fails is `read`; a change that fails must not read as if it only looked."""
    down = Gmail(error="upstream connect error")
    with pytest.raises(ToolError) as read:
        Mailbox(down.tool).ask("gmail_batchModify", {"ids": ["a"]})
    with pytest.raises(ToolError) as change:
        Mailbox(down.tool).ask("gmail_batchModify", {"ids": ["a"]}, changing=True)
    assert "changed" not in str(read.value)
    assert "could not be changed just now" in str(change.value)
    assert change.value.code == read.value.code == "network_error"

    signed_out = Gmail(error="invalid_grant: token has expired")
    with pytest.raises(ToolError) as renew:
        Mailbox(signed_out.tool).ask("gmail_batchModify", {}, changing=True)
    assert "mail cannot be changed until he does" in str(renew.value)
    assert renew.value.code == "unavailable"


def test_a_head_reads_the_servers_own_shape() -> None:
    """Sender, people, subject, time, an unescaped snippet, and with ``full`` text and files."""
    gmail = Gmail()
    gmail.add(
        "a1", "Plan", plain="Don't forget the plan.", cc="Kim <kim@example.com>",
        attachments=[("plan.pdf", "application/pdf", pdf(["Plan"]))],
    )
    box = Mailbox(gmail.tool)
    head = box.head("a1")
    assert (head.sender.name, head.sender.address) == ("Alex Wu", "alex@example.com")
    assert [one.address for one in head.to] == ["sam@example.com"]
    assert [one.address for one in head.cc] == ["kim@example.com"]
    assert head.subject == "Plan"
    assert head.at == gmail.messages["a1"].at
    assert head.snippet.startswith("Don't forget")  # Gmail sends "Don&#39;t"
    assert head.body == ""
    assert head.files == ()
    full = box.head("a1", full=True)
    assert full.body.startswith("Don't forget the plan.")
    assert [(one.name, one.mime) for one in full.files] == [("plan.pdf", "application/pdf")]
    assert full.files[0].size > 0


def test_heads_are_read_in_parallel_and_a_missing_message_is_none() -> None:
    """Eight reads in flight together get past the barrier; an id Gmail lacks is None."""
    gmail = _inbox(8)
    gmail.barrier = threading.Barrier(8)
    heads = Mailbox(gmail.tool).heads([f"m{n:03d}" for n in range(8)], full=True)
    assert [one.subject for one in heads if one] == [f"Note {n}" for n in range(8)]
    other = _inbox(2)
    assert Mailbox(other.tool).heads(["m000", "gone", "m001"])[1] is None


def test_a_head_of_an_id_gmail_lacks_says_which_id() -> None:
    """The error names the id and says how to get one, as a failure another argument fixes."""
    with pytest.raises(ToolError) as caught:
        Mailbox(Gmail().tool).head("nope")
    assert (caught.value.code, caught.value.kind) == ("not_found", "retry_other_args")
    assert "nope" in str(caught.value)


def test_a_small_message_is_read_raw_after_its_size_is_known() -> None:
    """``minimal`` first (size, labels, thread), then ``raw``; the attachment's bytes are read."""
    gmail = Gmail()
    gmail.add(
        "a1", "Statement", thread="t9", labels=PRIMARY, plain="Balance due $40.",
        attachments=[("march.pdf", "application/pdf", pdf(["Total due $40.00"]))],
    )
    read = Mailbox(gmail.tool).read("a1")
    assert [args["format"] for args in gmail.of("gmail_get")] == ["minimal", "raw"]
    assert (read.id, read.thread, read.files_read, read.why_not) == ("a1", "t9", True, "")
    assert "UNREAD" in read.labels
    assert read.mail.subject == "Statement"
    assert "Balance due $40." in read.mail.text
    assert read.mail.attachments[0].name == "march.pdf"
    assert read.mail.attachments[0].data.startswith(b"%PDF")


def test_a_message_past_the_limit_is_never_downloaded_raw() -> None:
    """Over ``raw_max_bytes`` it is read in the server's text form and says its files were not."""
    gmail = Gmail()
    gmail.add(
        "big", "Photos", plain="The pictures are attached.", size=30_000_000,
        attachments=[("trip.jpg", "image/jpeg", b"\xff\xd8" + b"0" * 500)],
    )
    read = Mailbox(gmail.tool, raw_max_bytes=15_000_000).read("big")
    assert [args["format"] for args in gmail.of("gmail_get")] == ["minimal", "full"]
    assert (read.files_read, read.why_not, read.size) == (False, "size", 30_000_000)
    assert "The pictures are attached." in read.mail.text
    assert [(one.name, one.mime) for one in read.mail.attachments] == [("trip.jpg", "image/jpeg")]
    assert read.mail.attachments[0].data == b""


def test_a_server_that_gives_no_raw_message_falls_back_to_its_text_form() -> None:
    """The same fallback, for a different reason, and the reason is named."""
    gmail = Gmail(raw_missing=True)
    gmail.add("a1", "Hello", plain="Short note.")
    read = Mailbox(gmail.tool).read("a1")
    assert (read.files_read, read.why_not) == (False, "no_raw")
    assert "Short note." in read.mail.text
    assert [args["format"] for args in gmail.of("gmail_get")] == ["minimal", "raw", "full"]


def test_headers_say_bulk_or_automated_and_are_kept_by_id() -> None:
    """The marks come from the raw headers, once; a message too big to read raw is unknown."""
    gmail = Gmail()
    gmail.add("news", "Weekly", headers={"List-Unsubscribe": "<mailto:u@example.com>"})
    gmail.add("bot", "Alert", headers={"Auto-Submitted": "auto-generated"})
    gmail.add("person", "Lunch?", headers={"Auto-Submitted": "no"})
    gmail.add("big", "Huge", size=99_000_000)
    box = Mailbox(gmail.tool, raw_max_bytes=1_000_000)
    found = box.envelopes(["news", "bot", "person", "big", "gone"])
    assert set(found) == {"news", "bot", "person", "big"}
    news, bot, person = found["news"], found["bot"], found["person"]
    assert news is not None
    assert (news.bulk, news.automated) == (True, False)
    assert bot is not None
    assert (bot.bulk, bot.automated) == (False, True)
    assert person is not None
    assert (person.bulk, person.automated) == (False, False)
    assert person.addressed_to({"sam@example.com"})
    assert found["big"] is None
    before = len(gmail.calls)
    assert box.envelopes(["news", "big"]) == {"news": news, "big": None}
    assert len(gmail.calls) == before


def test_his_own_addresses_are_the_from_of_his_sent_mail_kept_for_a_day() -> None:
    """One search of ``in:sent`` and the heads it found; asked again within a day: no call."""
    gmail = Gmail()
    gmail.add("s1", "Re: Plan", sender="Sam Lee <sam@example.com>", to=ALEX, labels=SENT)
    gmail.add("s2", "Hello", sender="Sam <Sam.Lee@Work.example.com>", to=ALEX, labels=SENT)
    gmail.add("r1", "Plan", sender=ALEX, to="Sam Lee <sam@example.com>")
    box = Mailbox(gmail.tool)
    mine = box.own(gmail.now)
    assert mine == {"sam@example.com", "sam.lee@work.example.com"}
    assert gmail.of("gmail_search")[0]["query"] == "in:sent"
    calls = len(gmail.calls)
    assert box.own(gmail.now + timedelta(hours=23)) == mine
    assert len(gmail.calls) == calls
    box.own(gmail.now + timedelta(hours=25))
    assert len(gmail.calls) > calls
