"""ADR 0359: the pages an answer rests on, and the list of them under her written answer.

A tool that finds pages for her answer (a deep web search) returns them as rows of ``sources``
(the pages its brief rests on) or ``pages`` (every page it found, when it stopped before a
brief). :func:`cited` reads those rows from a tool result, whatever the tool; :func:`section` writes
them as the list the devices draw under her written answer: a link on the title, then the site and
the page's date. The rows are web page data: the title and the address are cleaned so that nothing
in them can open a tag, close a link or make a second link.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from jarvis.shared.lang import t

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from jarvis.shared.lang import Language

TITLE_CHARS: Final[int] = 70
"""The most of a title a row keeps, in the result she reads and in the list under her answer."""

_ADDRESS: Final = re.compile(r"https?://[^\s<>\"\\]+", re.IGNORECASE)
_SEPARATOR: Final = " · "


@dataclass(frozen=True)
class Source:
    """A page a deep search found or rests on: title, site, the page's date if the search gave one.

    ``site`` is the host without ``www.``. ``date`` is the day as ``YYYY-MM-DD`` when the search's
    page age names one, else the age as the search wrote it, ``None`` when it gave none. ``url`` is
    whole: it is the link a device shows.
    """

    title: str
    site: str
    date: str | None
    url: str

    def row(self) -> dict[str, str]:
        """The row the model reads and the answer's list of sources is written from."""
        row = {"title": _line(self.title, TITLE_CHARS) or self.site}
        row["site"] = self.site
        if self.date:
            row["date"] = self.date
        row["url"] = self.url
        return row


def _line(text: str, limit: int) -> str:
    """One line of at most ``limit`` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"


def cited(payload: Mapping[str, Any]) -> tuple[Source, ...]:
    """The pages a tool result names for her answer, in order; none when it names none.

    A row without a web address is left out; the rest are read as the result gave them.
    """
    found: list[Source] = []
    for key in ("sources", "pages"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if not isinstance(url, str) or _ADDRESS.fullmatch(url) is None:
                continue
            site = str(row.get("site") or (urlsplit(url).hostname or "")).removeprefix("www.")
            date = row.get("date")
            found.append(Source(
                str(row.get("title") or site), site,
                date if isinstance(date, str) and date else None, url,
            ))
    return tuple(found)


def _plain(text: str, limit: int) -> str:
    """A page's own words as one line that cannot open a tag, a link or a second column."""
    line = _line(text.replace("<", "").replace(">", ""), limit)
    return line.replace("[", "(").replace("]", ")").replace(_SEPARATOR, " - ")


def section(sources: Iterable[Source], lang: Language | None = None) -> str:
    """The list under her written answer: a heading, then one row per page.

    Each row is ``- [title](address) · site · date``: the title is the link, and the site and the
    date follow it. A page's date is left out when the search gave none.
    """
    rows = []
    for source in sources:
        address = source.url.replace("(", "%28").replace(")", "%29").replace("'", "%27")
        site = _plain(source.site, TITLE_CHARS)
        parts = [f"[{_plain(source.title, TITLE_CHARS) or site}]({address})", site]
        if source.date:
            parts.append(_plain(source.date, TITLE_CHARS))
        rows.append("- " + _SEPARATOR.join(part for part in parts if part))
    return "\n".join([f"## {t('sources.heading', lang=lang)}", *rows]) if rows else ""
