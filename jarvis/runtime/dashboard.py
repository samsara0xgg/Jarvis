"""ADR 0147: what the Dashboard has open, and the reply draft under an open letter.

Both are in memory only: a restart forgets them; the event log keeps what Jarvis wrote as
the ``write_mail_draft`` rows.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable

FOCUS_STALE_S: Final = 60.0
"""The page re-posts its focus every 20 s while the item is open; older than this is closed."""
DRAFT_CHARS: Final = 8000
_DRAFTS_KEPT: Final = 20
DRAFT_LINE_PREFIX: Final = "Draft reply under it"
_DRAFT_LINE_BODY_CHARS: Final = 2000
DRAFT_LINE_CHARS: Final = len(DRAFT_LINE_PREFIX) + _DRAFT_LINE_BODY_CHARS + 60
"""The draft line is the one live-context line allowed past 200 characters."""
_SUBJECT_CHARS: Final = 80
_SENDER_CHARS: Final = 60

FocusKind = Literal["mail", "agent", "brief"]


class Draft(NamedTuple):
    """One reply draft: ``subject`` None means "Re: <the letter's subject>"; ``by`` jarvis|owner."""

    revision: int
    subject: str | None
    body: str
    by: str


class _Focus(NamedTuple):
    kind: FocusKind
    id: str
    title: str
    sender: str
    at: float


def _one_line(text: str, limit: int) -> str:
    """Titles come from mail and sessions: one line, no quote marks, capped."""
    return " ".join(text.split()).replace('"', "'")[:limit]


class FocusState:
    """The one item Allen has open on the Dashboard, or none."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with nothing open; ``clock`` reads monotonic seconds."""
        self._clock = clock
        self._now: _Focus | None = None

    def set(
        self, kind: FocusKind | None, ident: str = "", title: str = "", sender: str = "",
    ) -> None:
        """Open an item (or refresh the same one); ``kind`` None closes."""
        self._now = None if kind is None else _Focus(kind, ident, title, sender, self._clock())

    def _fresh(self) -> _Focus | None:
        now = self._now
        return now if now is not None and self._clock() - now.at <= FOCUS_STALE_S else None

    def mail_id(self) -> str | None:
        """The Gmail id of the open letter, or None."""
        now = self._fresh()
        return now.id if now is not None and now.kind == "mail" else None

    def line(self) -> str | None:
        """The state-block line (a ``live_context`` producer): titles only, never a body."""
        now = self._fresh()
        if now is None:
            return None
        if now.kind == "mail":
            sender = _one_line(now.sender, _SENDER_CHARS)
            return (
                f'Dashboard: Allen has this letter open: "{_one_line(now.title, _SUBJECT_CHARS)}"'
                f"{f' from {sender}' if sender else ''} (Gmail id {_one_line(now.id, 64)})."
                ' Words like "this email" mean it.'
            )
        if now.kind == "agent":
            return (
                f"Dashboard: Allen has an agent session open: "
                f"{_one_line(now.title, _SUBJECT_CHARS)} ({_one_line(now.id, 64)})."
            )
        return "Dashboard: Allen has the morning brief open."


class MailDrafts:
    """Reply drafts by letter id: Jarvis writes the whole body each time, Allen edits by hand."""

    def __init__(self, focus: FocusState) -> None:
        """Jarvis writes only under the letter ``focus`` has open."""
        self._focus = focus
        self._drafts: dict[str, Draft] = {}
        self._revision = 0

    def _store(self, letter_id: str, subject: str | None, body: str, by: str) -> Draft:
        self._revision += 1  # one counter for every change, so a page sees any change as new
        self._drafts.pop(letter_id, None)
        self._drafts[letter_id] = draft = Draft(self._revision, subject, body, by)
        while len(self._drafts) > _DRAFTS_KEPT:
            del self._drafts[next(iter(self._drafts))]
        return draft

    def write(self, letter_id: str, text: str) -> int:
        """Jarvis replaces the body under the open letter; the revision. ValueError if refused."""
        if self._focus.mail_id() != letter_id:
            msg = "that letter is not open on the Dashboard"
            raise ValueError(msg)
        text = text.strip()
        if not text or len(text) > DRAFT_CHARS:
            msg = f"the draft must be 1 to {DRAFT_CHARS} characters"
            raise ValueError(msg)
        before = self._drafts.get(letter_id)
        subject = None if before is None else before.subject
        return self._store(letter_id, subject, text, "jarvis").revision

    def edit(self, letter_id: str, subject: str, body: str) -> Draft:
        """Allen's own edit (subject and body) replaces the draft."""
        return self._store(letter_id, subject.strip() or None, body, "owner")

    def discard(self, letter_id: str) -> None:
        """Drop the draft."""
        self._drafts.pop(letter_id, None)

    def get(self, letter_id: str) -> Draft | None:
        """The latest draft under a letter, or None."""
        return self._drafts.get(letter_id)

    def line(self) -> str | None:
        """The state-block line (a ``live_context`` producer): the open letter's draft so far."""
        letter_id = self._focus.mail_id()
        draft = None if letter_id is None else self._drafts.get(letter_id)
        if draft is None or not draft.body.strip():
            return None
        who = "Allen" if draft.by == "owner" else "Jarvis"
        return (
            f"{DRAFT_LINE_PREFIX} (revision {draft.revision}, last edited by {who}): "
            f"{draft.body.strip()[:_DRAFT_LINE_BODY_CHARS]}"
        )
