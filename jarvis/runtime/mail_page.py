"""ADR 0148, 0366: the draft under the Dashboard's open letter is a Gmail draft.

The draft under a letter is the newest Gmail draft the ``mail`` tool wrote in that letter's
conversation; the event log holds it (``mail.draft_written``, ``mail.draft_closed``), so a restart
forgets nothing and the phone and the Mac see the same draft. His edit on the page writes a
revision through the tool's own code (a new Gmail draft that replaces the old), the page's Send
saves that edit and raises the ``mail`` send card for the draft, and Discard is the draft's undo.

Layer rules: runtime; the ``mail`` tool's draft code (``jarvis.execution.mail_letters``), the
event log and the Dashboard's view state.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import mail_letters
from jarvis.execution.mail_letters import Cause, Composed, Letters, Written
from jarvis.execution.mail_people import People
from jarvis.execution.tools import ToolError
from jarvis.runtime.dashboard import DRAFT_LINE_BODY_CHARS, DRAFT_LINE_PREFIX
from jarvis.shared import lang
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.runtime.dashboard import ViewState
    from jarvis.runtime.home import Home
    from jarvis.shared.clock import Clock

_ACTION: Final = "save"
_OWNER: Final = "owner"


class MailPage:
    """The reply draft under the letter open on the Dashboard, over his Gmail and the event log."""

    def __init__(self, view: ViewState, home: Home, event_log: Path, clock: Clock) -> None:
        """Read the open letter from ``view``, the letters from ``home``, drafts from the log."""
        self._view = view
        self._home = home
        self._log = event_log
        self._clock = clock
        self._letters = Letters(home.mailbox, People(home.mailbox))

    def _held(self, letter_id: str) -> tuple[list[Written], Written | None]:
        """Every draft in the letter's conversation, oldest first, and the newest open one."""
        thread = self._home.letter(letter_id)["thread_id"]
        with contextlib.closing(open_runtime_event_log(self._log)) as conn:
            held = [one for one in mail_letters.drafts(conn) if one.thread == thread]
        found = [one for one in held if one.is_open]
        return held, (found[-1] if found else None)

    @staticmethod
    def _shown(one: Written, revisions: int) -> dict[str, Any]:
        """The draft as the page reads it; ``revisions`` counts every draft in the conversation."""
        letter = one.letter
        to = ", ".join(letter.to)
        if letter.cc:
            to += lang.t("letter.cc_in", who=", ".join(letter.cc))
        if letter.bcc:
            to += lang.t("letter.bcc_in", who=", ".join(letter.bcc))
        return {
            "revision": revisions, "draft": one.short, "to": to, "subject": letter.subject,
            "body": letter.body, "by": "owner" if one.by == _OWNER else "jarvis",
        }

    def view(self, letter_id: str) -> dict[str, Any]:
        """``{draft: {revision, draft, to, subject, body, by} | None}`` for the open letter.

        ``revision`` climbs with every draft written in the conversation, so a page sees any
        change as new; ``draft`` is the number the ``mail`` tool's ``send`` and ``replaces`` take.
        """
        held, one = self._held(letter_id)
        return {"draft": None if one is None else self._shown(one, len(held))}

    def save(self, letter_id: str, subject: str, body: str) -> dict[str, Any]:
        """His edit of subject and body: a revision of the draft, or a reply if there is none.

        Nothing is written when the letter is as it was. ValueError when the letter cannot be
        made (the route's 400).
        """
        try:
            return self._save(letter_id, subject.strip(), body)
        except ToolError as exc:
            if exc.code == "invalid_arguments":
                raise ValueError(str(exc)) from exc
            raise

    def _save(self, letter_id: str, subject: str, body: str) -> dict[str, Any]:
        held, one = self._held(letter_id)
        now = self._clock.now()
        if one is None:
            composed = self._letters.compose(
                {"subject": subject, "body": body}, letter_id, now, self._clock.zone, _ACTION,
            )
        else:
            letter = replace(one.letter, subject=subject or one.letter.subject, body=body)
            who = self._letters.parse(letter, now)
            if problem := Letters.problem(who, letter.subject, letter.body, _ACTION):
                raise ToolError(problem, code="invalid_arguments")
            if letter.same_as(one.letter):
                return {"draft": self._shown(one, len(held))}
            composed = Composed(letter, who, one.reply_to)
        with contextlib.closing(open_runtime_event_log(self._log)) as conn:
            self._letters.write(conn, composed, Cause(actor="user", by=_OWNER), replaces=one)
        return self.view(letter_id)

    def sendable(self, letter_id: str, subject: str, body: str) -> str:
        """Save his edit, and give the number of the draft a send takes. ValueError if empty."""
        if not body.strip():
            msg = "the draft is empty"
            raise ValueError(msg)
        shown = self.save(letter_id, subject, body)["draft"]
        if shown is None:
            msg = "there is no draft to send"
            raise ValueError(msg)
        return str(shown["draft"])

    def discard(self, letter_id: str) -> None:
        """The draft's undo: move it to Trash. RuntimeError (the route's 502) when Gmail refuses."""
        _, one = self._held(letter_id)
        if one is None:
            return
        with contextlib.closing(open_runtime_event_log(self._log)) as conn:
            moved = self._letters.discard(conn, one, Cause(actor="user"))
        if not moved:
            msg = "Gmail would not move the draft to Trash, so it is still in his Drafts"
            raise RuntimeError(msg)

    def line(self) -> str | None:
        """The state-block line (a ``live_context`` producer): the open letter's draft so far."""
        letter_id = self._view.mail_id()
        if letter_id is None:
            return None
        try:
            _, one = self._held(letter_id)
        except (LookupError, ToolError, sqlite3.Error):
            return None
        if one is None or not one.letter.body.strip():
            return None
        who = "Allen" if one.by == _OWNER else "Jarvis"
        return (
            f"{DRAFT_LINE_PREFIX} (draft {one.short}, last edited by {who}): "
            f"{one.letter.body.strip()[:DRAFT_LINE_BODY_CHARS]}"
        )
