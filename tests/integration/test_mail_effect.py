"""ADR 0361, 0362: each ``mail`` call has its own level, through the real ``decide()`` loop.

A scripted model calls the real ``mail`` tool over the Gmail fake of ``tests.support``. A read and
an archive run with no card (the archive with its undo); a draft fails until ADR 0366 builds it;
a send cannot be completed yet, so it makes no card either, and Gmail is not asked for anything
that writes. Asserts on the rows a turn leaves, the receipts, and what the model was sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from jarvis.decision import DecideContext, LifecycleLike, RuntimePathsLike, ToolRegistryLike
from jarvis.decision.confirm_grammar import load_confirm_grammar
from jarvis.execution.mail_gmail import Mailbox
from jarvis.execution.mail_tool import build_mail_tool
from jarvis.execution.tools import ActionLifecycle, build_default_registry
from jarvis.shared.realtime import Wave1FeatureFlags
from jarvis.state.event_log import iter_events_of_types, open_event_log
from tests.support.consent import REPO, tool_results
from tests.support.gmail_fake import Gmail
from tests.support.mail_raw import SAM
from tests.support.model import Call, Reply, ScriptedModel
from tests.support.plugin_approval import Paths, logged_dicts, say

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.decision.llm import LLMClient

NOW = datetime.now(UTC)
NOT_YET = (
    "mail: writing and sending a letter is not available yet; tell him you cannot write or send"
    " mail just now"
)


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


def test_a_draft_is_a_small_change_and_fails_until_it_is_built(tmp_path: Path) -> None:
    """A draft is L1, so no card; the stub fails it with a receipt and nothing reaches Gmail."""
    gmail = _mailbox()
    llm = ScriptedModel(_call(action="draft", to="Sam", body="Thursday works"), "I can't.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "write Sam back", "T1") == "I can't."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        (proposed,) = logged_dicts(ctx.conn, "action.proposed")
        assert proposed["risk_level"] == "L1"
        (result,) = tool_results(llm, 1)
        assert (result["is_error"], result["kind"]) == (True, "impossible")
        assert result["code"] == "unavailable"
        assert result["error"] == NOT_YET
        assert gmail.calls == []
    finally:
        ctx.conn.close()


def test_a_send_makes_no_card_while_it_cannot_be_completed_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """A send is L3: its completion fails first, so no card, no proposal, a failed receipt."""
    gmail = _mailbox()
    llm = ScriptedModel(_call(action="send", to="Sam", subject="Re: Plan", body="Yes"), "I can't.")
    ctx = _context(tmp_path, llm, gmail)
    try:
        assert say(ctx, "tell Sam yes", "T1") == "I can't."
        assert logged_dicts(ctx.conn, "confirmation.requested") == []
        assert logged_dicts(ctx.conn, "action.proposed") == []
        (result,) = tool_results(llm, 1)
        assert (result["is_error"], result["kind"]) == (True, "impossible")
        assert result["code"] == "unavailable"
        assert result["error"] == NOT_YET
        assert result["receipt"].startswith("r")
        assert gmail.calls == []
    finally:
        ctx.conn.close()
