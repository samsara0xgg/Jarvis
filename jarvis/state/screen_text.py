"""What his Mac's screen said, window by window, read from TimeSink's shipped captures.

The writer of the doing lane may ask for it while it writes a day: one key of his attention (or
every window) over a stretch of the day, as the text TimeSink read off the screen. Each capture
row holds the text of one window at one moment and the span it was taken in; the span names the
key it is summed by (:func:`jarvis.state.attention.held`).

The text is given whole. Only a line identical to one already kept for the same window (app and
title) in the same read is left out; nothing is merged that differs by a character, and nothing
is merged across windows. Each kept line carries the moment it was first seen. :func:`seen` reads
the windows of a stretch, :func:`windows` folds capture rows into them.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import attention, imports, timesink
from jarvis.state.day_line import shipped_conn

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

    from jarvis.state.day_line import DaySources

REACH_MS: Final = attention.REACH_MS
"""How long before a stretch a capture is looked for that may still have been on screen in it."""


class Capture(NamedTuple):
    """One capture row: a window's text from when it was taken to when it was last seen."""

    at_ms: int
    last_ms: int
    app: str
    title: str
    key: str
    text: str


class Window(NamedTuple):
    """A window read over a stretch: its app and title, its key, and its lines, first seen first.

    ``lines`` are ``(first seen ms, text)``; ``first_ms`` and ``last_ms`` bound when its captures
    were on screen inside the stretch.
    """

    app: str
    title: str
    key: str
    first_ms: int
    last_ms: int
    lines: list[tuple[int, str]]


def lines_of(text: str) -> list[str]:
    """A capture's text as lines: whitespace inside a line folded, blank lines left out."""
    return [one for raw in text.splitlines() if (one := " ".join(raw.split()))]


def windows(captures: Iterable[Capture], start_ms: int, end_ms: int) -> list[Window]:
    """``captures`` on screen inside ``[start_ms, end_ms)``, folded into windows by first sight.

    A window is one app and title. Its lines are every line of its captures, in the order taken,
    less a line identical to one it already holds; each keeps the moment it was first seen, the
    capture's time or the stretch's start for one taken before it.
    """
    held: dict[tuple[str, str], Window] = {}
    known: dict[tuple[str, str], set[str]] = {}
    for c in sorted(captures, key=lambda c: (c.at_ms, c.app, c.title)):
        if not (c.at_ms < end_ms and max(c.last_ms, c.at_ms + 1) > start_ms):
            continue
        at = max(c.at_ms, start_ms)
        ident = (c.app, c.title)
        window = held.get(ident)
        if window is None:
            window = held[ident] = Window(c.app, c.title, c.key, at, at, [])
            known[ident] = set()
        held[ident] = window._replace(last_ms=max(window.last_ms, min(c.last_ms, end_ms), at))
        lines = held[ident].lines
        for line in lines_of(c.text):
            if line not in known[ident]:
                known[ident].add(line)
                lines.append((at, line))
    return sorted(held.values(), key=lambda w: (w.first_ms, w.app, w.title))


def _ms(value: object) -> int:
    return round(datetime.fromisoformat(timesink.moment(str(value))).timestamp() * 1000)


def captures(
    shipped: sqlite3.Connection, start_ms: int, end_ms: int, spans: Mapping[str, Any],
) -> list[Capture]:
    """The Mac's capture rows that may have been on screen in ``[start_ms, end_ms)``.

    ``spans`` are the fields of the TimeSink spans by id; a capture is keyed by the span it was
    taken in, else by its own app and title.
    """
    found: list[Capture] = []
    for row in imports.newest(
        shipped, "timesink", start_ms=start_ms - REACH_MS, end_ms=end_ms,
        kinds=("capture",), by_prefix=True,
    ):
        f = row.fields
        if not f.get("at"):
            continue
        at = _ms(f["at"])
        last = _ms(f["lastSeenAt"]) if f.get("lastSeenAt") else at
        app, title = str(f.get("appName") or "unknown app"), str(f.get("title") or "")
        span = spans.get(str(f.get("spanID")))
        (key_app, what), _ = attention.held(span or {"appName": app, "title": title})
        key = f"mac|{key_app}|{what}"
        found.append(Capture(at, last, app, title, key, str(f.get("text") or "")))
    return found


def _spans(shipped: sqlite3.Connection, start_ms: int, end_ms: int) -> dict[str, Any]:
    return {
        str(row.fields.get("id")): row.fields
        for row in imports.newest(
            shipped, "timesink", start_ms=start_ms - 2 * REACH_MS, end_ms=end_ms,
            kinds=("span",), by_prefix=True,
        )
    }


def seen(
    sources: DaySources, start_ms: int, end_ms: int, key: str | None = None,
) -> list[Window]:
    """The windows on his Mac's screen in ``[start_ms, end_ms)``, only ``key``'s when given.

    None when nothing was shipped. A read that fails raises; the caller says so to the writer.
    """
    if end_ms <= start_ms:
        return []
    conn = shipped_conn(sources)
    if conn is None:
        return []
    with closing(conn):
        rows = captures(conn, start_ms, end_ms, _spans(conn, start_ms, end_ms))
    return windows((c for c in rows if key is None or c.key == key), start_ms, end_ms)


__all__ = ["REACH_MS", "Capture", "Window", "captures", "lines_of", "seen", "windows"]
