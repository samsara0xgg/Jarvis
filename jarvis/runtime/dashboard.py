"""ADR 0176: what the Dashboard shows, kept in memory: a restart forgets it.

The reply draft under an open letter is a Gmail draft the ``mail`` tool wrote, read from the event
log (ADR 0366, ``jarvis.runtime.mail_page``); only its line's prefix and size are fixed here.
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
DRAFT_LINE_PREFIX: Final = "Draft reply under it"
DRAFT_LINE_BODY_CHARS: Final = 2000
DRAFT_LINE_CHARS: Final = len(DRAFT_LINE_PREFIX) + DRAFT_LINE_BODY_CHARS + 60
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
        self, page: str, item_id: str | None, *, asked: bool = True, position: int | None = None,
    ) -> dict[str, str | None]:
        """Ask the companion to turn to ``page``, and to one item on it when the view carries it.

        The item is ``item_id`` (the open item or a row of the current view, or on ``settings``
        a Settings category, ADR 0208; any other id opens the page alone) or ``position``, its
        number among the rows the Dashboard shows on that page, counted from 1 as the state
        block's line numbers them. A position can be read only while the Dashboard is on that
        page: from anywhere else the page opens alone. ``asked`` is whether Allen's words asked
        to see something: without it a shut Dashboard stays shut. Returns what was sent;
        ValueError, worded for the model with the way out, for an unknown page, both an id and
        a position, a position past the rows, a shut Dashboard he did not ask for, or when no
        Dashboard link is attached.
        """
        if page not in PAGES and page != CLOSE:
            msg = f"{page!r} is not a Dashboard page; use one of: {', '.join((*PAGES, CLOSE))}"
            raise ValueError(msg)
        if self.push is None:
            msg = "the Dashboard is not connected, so nothing can be shown on it; answer in words"
            raise ValueError(msg)
        if not asked and page != CLOSE and self._fresh() is None:
            msg = (
                "the Dashboard is shut: answer him without it, and open a page only when he "
                "asks to see something (show me, open it)"
            )
            raise ValueError(msg)
        if item_id and position is not None:
            msg = "pass item (a number on the page) or item_id (an exact id), not both"
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
        if position is not None:
            known = self._row(page, position)
            shown = known.id if known else None
        else:
            known = self.knows(item_id) if item_id else None
            shown = item_id if known else None
        sent = {"page": page, "item_id": shown, "kind": None if known is None else known.kind}
        self.push(sent)
        return sent

    def _row(self, page: str, position: int) -> Item | None:
        """Row ``position`` (from 1) of the list shown on ``page``, or None when it is not shown.

        ValueError when the Dashboard is on ``page`` and has no such row.
        """
        now = self._fresh()
        if now is None or now.page != page:
            return None
        way_out = "pass an exact id from a tool result as item_id, or ask Allen which one"
        if not now.rows:
            msg = f"page {page} shows no numbered list now, so item={position} does not exist; "
            raise ValueError(msg + way_out)
        if position > len(now.rows):
            more = " (only its first ten are reported to you)" if len(now.rows) == VIEW_ROWS else ""
            msg = f"page {page} shows {len(now.rows)} items{more}; item={position} does not exist; "
            raise ValueError(msg + way_out)
        return now.rows[position - 1]

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
