"""ADR 0361, 0362, 0366: each ``mail`` call has its own level, through the real ``decide()`` loop.

A scripted model calls the real ``mail`` tool over the Gmail fake of ``tests.support``. A read, an
archive and a draft run with no card (the archive and the draft with their undo); a send is
completed into a card that shows the letter, writes nothing until the button, and then sends it.
Asserts on the rows a turn leaves, the receipts, the card the clients read and what the model was
sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision import DecideContext, LifecycleLike, RuntimePathsLike, ToolRegistryLike
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.runtime.daemon.cards import confirmation_card
from jarvis.shared import lang
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import iter_events_of_types, open_event_log
from tests.support.consent import REPO, tool_results, waiting_card
from tests.support.gmail_fake import SENT, Gmail
from tests.support.mail_raw import SAM
from tests.support.model import Call, Reply, ScriptedModel
from tests.support.plugin_approval import Paths, logged_dicts, press, say

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _english() -> Iterator[None]:
    """The questions of a card are in English; the language is put back."""
    before = lang.language()
    lang.set_language("en")
    yield
    lang.set_language(before)


def _mailbox() -> Gmail:
    gmail = Gmail(now=NOW)
    gmail.add("p0", "Plan 0", sender=SAM, at=NOW - timedelta(minutes=10))
    gmail.add("p1", "Plan 1", sender=SAM, at=NOW - timedelta(minutes=20))
    return gmail


def _context(tmp_path: Path, llm: ScriptedModel, gmail: Gmail) -> DecideContext:
    """A ``decide()`` context over the default registry with the real ``mail`` tool added."""
    registry = build_default_registry(confirmation_dispatch_outbox=True)
    registry.register(*build_mail_tool(Mailbox(gmail.tool)))
    paths = Paths(event_log=tmp_path / "events.db", artifacts_root=tmp_path / "artifacts")
    paths.artifacts_root.mkdir(parents=True, exist_ok=True)
    return DecideContext(
        conn=open_event_log(paths.event_log),
        runtime_paths=cast("RuntimePathsLike", paths),
        tool_registry=cast("ToolRegistryLike", registry),
        lifecycle=cast("LifecycleLike", ActionLifecycle()),
        llm_client=cast("LLMClient", llm),
        system_prompt="stub system prompt",
        confirm_grammar_table=load_confirm_grammar(REPO / "config" / "confirm_grammar.yaml"),
        wave1_features=Wave1FeatureFlags(confirmation_dispatch_outbox=True),
    )


def _call(**args: Any) -> Reply:  # noqa: ANN401 — the model's arguments.
    return Reply(calls=(Call("mail", args, "c0"),))


def test_a_read_runs_with_no_card_and_its_receipt_says_what_it_covered(tmp_path: Path) -> None:
    """A list is L0: proposed as a read, run, receipted with the window it looked at."""
    gmail = _mailbox()
    llm = ScriptedModel(_call(), "Two unread.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "any new mail", "T1") == "Two unread."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L0"
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert done["receipt"]["reads_only"] is True
        assert done["receipt"]["covered"]["window"] == "mail, unread in his inbox"
    finally:
        ctx.conn.close()


def test_an_archive_runs_with_no_card_and_gives_the_undo_that_puts_it_back(
    tmp_path: Path,
) -> None:
    """An archive is L1: one Gmail change, one event, and a receipt whose undo names it."""
    gmail = _mailbox()
    llm = ScriptedModel(_call(action="archive", message="p0"), "Archived.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "archive the first", "T1") == "Archived."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L1"
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        (event,) = iter_events_of_types(ctx.conn, ["mail.inbox_changed"])
        assert done["receipt"]["undo"] == event.event_uid
        assert done["receipt"]["object"] == "mail:p0"
        changed = {"messageIds": ["p0"], "removeLabelIds": ["INBOX"]}
        assert gmail.of("gmail_batchModify") == [changed]
    finally:
        ctx.conn.close()


def test_a_draft_is_a_small_change_that_runs_with_no_card_and_gives_its_undo(
    tmp_path: Path,
) -> None:
    """A draft is L1: a Gmail draft and one event, a receipt whose undo names it; none is sent."""
    gmail = _mailbox()
    draft = _call(action="draft", to="Sam", subject="Plan", body="Thursday works")
    llm = ScriptedModel(draft, "Done.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "write Sam back", "T1") == "Done."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L1"
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        (event,) = iter_events_of_types(ctx.conn, ["mail.draft_written"])
        assert done["receipt"]["undo"] == event.event_uid
        assert done["receipt"]["object"] == f"draft:{event.event_uid[:8]}"
        assert gmail.names().count("gmail_createDraft") == 1
        assert "gmail_sendDraft" not in gmail.names()
    finally:
        ctx.conn.close()


def _send(**args: Any) -> Reply:  # noqa: ANN401 — the model's arguments.
    return _call(action="send", **args)


def test_a_send_is_completed_into_a_card_and_writes_nothing_until_the_button(
    tmp_path: Path,
) -> None:
    """The card is asked with the letter made (names found, thread, who is his); Gmail is read."""
    gmail = _mailbox()
    llm = ScriptedModel(_send(to="Sam", subject="Re: Plan", body="Yes"), "Shall I?")
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "tell Sam yes", "T1")
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        (frozen,) = card["calls"]
        assert frozen["args_meta"] == {
            "action": "send", "to": ["Sam Lee <sam@example.com>"], "cc": [], "bcc": [],
            "subject": "Re: Plan", "body": "Yes", "thread": "", "draft": "", "you": [],
        }
        assert frozen["risk_level"] == "L3"
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        assert set(gmail.names()) <= {"gmail_search", "gmail_get"}  # nothing written yet
    finally:
        ctx.conn.close()


def test_the_button_sends_the_frozen_letter_with_one_new_draft_and_one_send(tmp_path: Path) -> None:
    """Accept: the lease, the leased run, a new draft and its send, once each."""
    gmail = _mailbox()
    llm = ScriptedModel(_send(to="Sam", subject="Re: Plan", body="Yes"), "Shall I?", "Sent.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "tell Sam yes", "T1")
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        press(ctx, {"confirmation_id": card["confirmation_id"], "decision": "accept"}, "T2")
        (done,) = logged_dicts(ctx.conn, "action.result_observed")
        assert done["receipt"]["status"] == "done"
        assert done["receipt"]["object"] == "mail:d1"
        assert [n for n in gmail.names() if n in {"gmail_createDraft", "gmail_sendDraft"}] == [
            "gmail_createDraft", "gmail_sendDraft",
        ]
        assert gmail.of("gmail_createDraft") == [
            {"to": "sam@example.com", "subject": "Re: Plan", "body": "Yes"},
        ]
    finally:
        ctx.conn.close()


def test_a_letter_he_edits_on_the_card_is_the_letter_that_goes(tmp_path: Path) -> None:
    """The Mac's edit of subject and body re-freezes the call; the send holds the edit."""
    gmail = _mailbox()
    llm = ScriptedModel(_send(to="Sam", subject="Re: Plan", body="Yes"), "Shall I?", "Sent.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "tell Sam yes", "T1")
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        edits = {"body": "Yes, at noon.", "subject": "Re: Plan 2"}
        press(ctx, {
            "confirmation_id": card["confirmation_id"], "decision": "accept", "edits": edits,
        }, "T2")
        assert [c["subject"] for c in gmail.of("gmail_createDraft")] == ["Re: Plan 2"]
        assert gmail.of("gmail_createDraft")[0]["body"] == "Yes, at noon."
        assert len(gmail.of("gmail_sendDraft")) == 1
        revised = logged_dicts(ctx.conn, "confirmation.requested")[-1]["calls"][0]["args_meta"]
        assert (revised["subject"], revised["body"]) == ("Re: Plan 2", "Yes, at noon.")
    finally:
        ctx.conn.close()


def test_a_refused_card_sends_nothing_and_writes_nothing(tmp_path: Path) -> None:
    """Reject: no draft, no send."""
    gmail = _mailbox()
    llm = ScriptedModel(_send(to="Sam", subject="Re: Plan", body="Yes"), "Shall I?", "OK.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "tell Sam yes", "T1")
        (card,) = logged_dicts(ctx.conn, "confirmation.requested")
        press(ctx, {"confirmation_id": card["confirmation_id"], "decision": "reject"}, "T2")
        assert logged_dicts(ctx.conn, "action.result_observed") == []
        assert set(gmail.names()) <= {"gmail_search", "gmail_get"}
    finally:
        ctx.conn.close()


def test_a_name_that_matches_two_people_makes_no_card_and_asks_him_which(tmp_path: Path) -> None:
    """The completion fails with needs_him naming both; nothing is proposed, nothing written."""
    gmail = _mailbox()
    gmail.add("wu", "Notes", sender="Sam Wu <sam.wu@example.org>", at=NOW - timedelta(days=4))
    llm = ScriptedModel(_send(to="Sam", subject="Re: Plan", body="Yes"), "Which Sam?")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "tell Sam yes", "T1") == "Which Sam?"
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        assert logged_dicts(ctx.conn, "action.proposed") == []
        (result,) = tool_results(llm, 1)
        assert (result["is_error"], result["kind"]) == (True, "needs_him")
        assert "Sam Wu <sam.wu@example.org>" in result["error"]
        assert set(gmail.names()) <= {"gmail_search", "gmail_get"}
    finally:
        ctx.conn.close()


def _own_mailbox() -> Gmail:
    """The mailbox, with one old sent mail that tells which address is his."""
    gmail = _mailbox()
    gmail.add(
        "sent0", "Hello", sender="Allen <allen@example.com>", to=SAM, labels=SENT,
        at=NOW - timedelta(days=40),
    )
    return gmail


def test_the_card_the_clients_read_lists_every_recipient_with_his_own_marked(
    tmp_path: Path,
) -> None:
    """To, Cc and Bcc by name and address, (you) after his own, the whole body, and the question."""
    gmail = _own_mailbox()
    llm = ScriptedModel(
        _send(
            to=["Sam", "me"], cc="kim@example.com", bcc="pat@example.com", subject="Plans",
            body="Noon.",
        ),
        "Shall I?",
    )
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "email Sam and me", "T1")
        slot = waiting_card(ctx)
        assert slot is not None
        card = confirmation_card(slot, "mac")
        assert (card["tool"], card["action"], card["letter"]) == ("mail", "send this email", True)
        assert card["args"] == {
            "to": [
                "Sam Lee <sam@example.com>", "allen@example.com (you)",
                "Cc kim@example.com", "Bcc pat@example.com",
            ],
            "subject": "Plans", "body": "Noon.",
        }
        assert slot.template_line == (
            "The email to Sam Lee, allen@example.com (you) (Cc kim@example.com)"
            ' (Bcc pat@example.com) is ready, subject "Plans". Send it?'
        )
    finally:
        ctx.conn.close()


def test_a_reply_card_carries_the_thread_and_his_edit_is_asked_and_sent_as_edited(
    tmp_path: Path,
) -> None:
    """``threadId`` is the answered conversation; the new question names the edited subject."""
    gmail = _own_mailbox()
    llm = ScriptedModel(_send(reply_to="p0", body="Yes"), "Shall I?", "Sent.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        say(ctx, "tell Sam yes", "T1")
        slot = waiting_card(ctx)
        assert slot is not None
        assert confirmation_card(slot, None)["args"] == {
            "to": ["Sam Lee <sam@example.com>"], "subject": "Re: Plan 0", "body": "Yes",
            "threadId": "p0",
        }
        edits = {"body": "Yes, noon.", "subject": "Re: Plan 0 (noon)"}
        press(ctx, {
            "confirmation_id": slot.confirmation_id, "decision": "accept", "edits": edits,
        }, "T2")
        question = logged_dicts(ctx.conn, "confirmation.requested")[-1]["calls"][0]["question"]
        assert question == 'The email to Sam Lee is ready, subject "Re: Plan 0 (noon)". Send it?'
        (made,) = gmail.of("gmail_createDraft")
        assert (made["subject"], made["body"], made["threadId"]) == (
            "Re: Plan 0 (noon)", "Yes, noon.", "p0",
        )
    finally:
        ctx.conn.close()
