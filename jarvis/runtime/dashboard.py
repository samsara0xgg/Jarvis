"""ADR 0176, 0148: what the Dashboard shows, and the reply draft under an open letter.

Both are in memory only: a restart forgets them; the event log keeps what Jarvis wrote as
the ``write_mail_draft`` rows.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

VIEW_STALE_S: Final = 60.0
"""The shell re-posts its view every 20 s while the panel is open; older than this is closed."""
VIEW_ROWS: Final = 10
VIEW_LINE_PREFIX: Final = "Dashboard: "
VIEW_LINE_CHARS: Final = 1200
"""The view line is the second live-context line allowed past 200 characters (ADR 0176)."""
DRAFT_CHARS: Final = 8000
_DRAFTS_KEPT: Final = 20
DRAFT_LINE_PREFIX: Final = "Draft reply under it"
_DRAFT_LINE_BODY_CHARS: Final = 2000
DRAFT_LINE_CHARS: Final = len(DRAFT_LINE_PREFIX) + _DRAFT_LINE_BODY_CHARS + 60
"""The draft line is the one live-context line allowed past 200 characters (ADR 0148)."""
_TITLE_CHARS: Final = 80
_ID_CHARS: Final = 64
_WORD_CHARS: Final = 24

PAGES: Final = (
    "home", "conversation", "now", "agents", "usage", "plugins", "projects", "settings", "brief",
    "mail", "memory", "jobs",
)
SETTINGS_CATEGORIES: Final = (
    "general", "home", "look", "voice", "board", "sounds", "agents", "privacy", "accounts",
    "devices", "models", "advanced",
)
"""The Settings categories ``show_on_dashboard`` can open (ADR 0208): ``cats`` in
SettingsPage.tsx. ``devices`` is the one that pairs a phone."""
# Not a page: she folds the Dashboard away when Allen asks to close it.
CLOSE: Final = "close"
"""The pages ``show_on_dashboard`` can turn to: ``Page`` in AroundDashboard.tsx, less the
home arranger."""
_PAGE_NAMES: Final = {
    "home": "home screen", "conversation": "Conversation page", "now": "Right now page",
    "agents": "Agents page", "usage": "Usage page", "plugins": "Plugins page",
    "projects": "Projects page", "settings": "Settings page", "arrange": "Arrange page",
    "brief": "Morning brief page", "mail": "Mail page", "memory": "Memory page",
    "jobs": "Job mail page",
}


class Item(NamedTuple):
    """One thing on screen: a letter, a note, a session, a row (kind "row" in a list).

    ``mail_id`` is the Gmail id of the mail behind a Jobs-page row ("" for everything else).
    """

    kind: str
    id: str
    title: str
    mail_id: str = ""


class _View(NamedTuple):
    page: str
    tab: str
    item: Item | None
    rows: tuple[Item, ...]
    at: float


class Draft(NamedTuple):
    """One reply draft: ``subject`` None means "Re: <the letter's subject>"; ``by`` jarvis|owner."""

    revision: int
    subject: str | None
    body: str
    by: str


def _one_line(text: str, limit: int) -> str:
    """Titles come from mail, notes and sessions: one line, no quote marks, capped."""
    return " ".join(text.split()).replace('"', "'")[:limit]


def _clean(kind: str, ident: str, title: str, mail_id: str = "") -> Item:
    return Item(
        _one_line(kind, _WORD_CHARS),
        _one_line(ident, _ID_CHARS),
        _one_line(title, _TITLE_CHARS),
        _one_line(mail_id, _ID_CHARS),
    )


class ViewState:
    """What the Dashboard shows now (page, tab, open item, up to ten rows), or nothing.

    The one thing the shell reports (ADR 0176); the mail page's open letter is the open item
    of kind ``mail`` (ADR 0148). ``push`` is how a ``present`` op reaches the companion; the
    daemon sets it once its WebSocket broadcaster exists.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        """Start with nothing on screen; ``clock`` reads monotonic seconds."""
        self._clock = clock
        self._now: _View | None = None
        self.push: Callable[[dict[str, str | None]], None] | None = None

    def set(
        self,
        page: str | None,
        tab: str = "",
        item: tuple[str, str, str] | None = None,
        rows: Sequence[tuple[str, ...]] = (),
    ) -> None:
        """Replace the view (or refresh it); ``page`` None means the panel is closed.

        ``item`` is ``(kind, id, title)``, ``rows`` are ``(id, title)`` or
        ``(id, title, mail_id)`` in screen order; titles are cut to one line of 80 characters
        and only ten rows are kept.
        """
        if page is None:
            self._now = None
            return
        self._now = _View(
            _one_line(page, _WORD_CHARS),
            _one_line(tab, _WORD_CHARS),
            None if item is None else _clean(*item),
            tuple(_clean("row", *row) for row in rows[:VIEW_ROWS]),
            self._clock(),
        )

    def _fresh(self) -> _View | None:
        now = self._now
        return now if now is not None and self._clock() - now.at <= VIEW_STALE_S else None

    def mail_id(self) -> str | None:
        """The Gmail id of the letter open on the Mail page, or None."""
        now = self._fresh()
        item = None if now is None else now.item
        return item.id if item is not None and item.kind == "mail" else None

    def knows(self, item_id: str) -> Item | None:
        """The open item or row of the current view with this id or mail id, or None."""
        now = self._fresh()
        if now is None:
            return None
        return next(
            (
                one
                for one in (now.item, *now.rows)
                if one is not None and item_id in (one.id, one.mail_id)
            ),
            None,
        )

    def present(
        self, page: str, item_id: str | None, *, asked: bool = True
    ) -> dict[str, str | None]:
        """Ask the companion to turn to ``page``, and to ``item_id`` when the view carries it.

        On ``settings``, ``item_id`` may also be a Settings category (ADR 0208). An id the
        current view does not carry opens the page alone. ``asked`` is whether
        Allen's words asked to see something: without it a shut Dashboard stays shut. Returns
        what was sent; ValueError for an unknown page, a shut Dashboard he did not ask for, or
        when no Dashboard link is attached.
        """
        if page not in PAGES and page != CLOSE:
            msg = f"unknown page {page!r}"
            raise ValueError(msg)
        if self.push is None:
            msg = "the Dashboard is not connected"
            raise ValueError(msg)
        if not asked and page != CLOSE and self._fresh() is None:
            msg = "the Dashboard is shut and Allen did not ask to see anything; answer without it"
            raise ValueError(msg)
        if page == CLOSE:
            closed: dict[str, str | None] = {"page": None, "item_id": None, "kind": None}
            self.push(closed)
            return closed
        sent: dict[str, str | None]
        if page == "settings" and item_id in SETTINGS_CATEGORIES:
            sent = {"page": page, "item_id": item_id, "kind": "category"}
            self.push(sent)
            return sent
        known = self.knows(item_id) if item_id else None
        sent = {
            "page": page,
            "item_id": item_id if known else None,
            "kind": None if known is None else known.kind,
        }
        self.push(sent)
        return sent

    def line(self) -> str | None:
        """The state-block line (a ``live_context`` producer): titles and ids, never a body."""
        now = self._fresh()
        if now is None:
            return None
        head = f"{VIEW_LINE_PREFIX}Allen is on the {_PAGE_NAMES.get(now.page, now.page + ' page')}"
        if now.tab:
            head += f" (tab {now.tab})"
        item = now.item
        if item is not None:
            head += f' with "{item.title}" open ({item.kind} {item.id})'
        head += "."
        if item is not None and item.kind == "mail":
            head += ' Words like "this email" mean it.'
        text = head
        if now.rows:
            text += " On screen:"
        for number, row in enumerate(now.rows, 1):
            mail = f", mail {row.mail_id}" if row.mail_id else ""
            piece = f' {number}. "{row.title}" ({row.id}{mail})'
            if len(text) + len(piece) > VIEW_LINE_CHARS:
                break
            text += piece
        return text


class MailDrafts:
    """Reply drafts by letter id: Jarvis writes the whole body each time, Allen edits by hand."""

    def __init__(self, view: ViewState) -> None:
        """Jarvis writes only under the letter ``view`` has open."""
        self._view = view
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
        if self._view.mail_id() != letter_id:
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
        letter_id = self._view.mail_id()
        draft = None if letter_id is None else self._drafts.get(letter_id)
        if draft is None or not draft.body.strip():
            return None
        who = "Allen" if draft.by == "owner" else "Jarvis"
        return (
            f"{DRAFT_LINE_PREFIX} (revision {draft.revision}, last edited by {who}): "
            f"{draft.body.strip()[:_DRAFT_LINE_BODY_CHARS]}"
        )
