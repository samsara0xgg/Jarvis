"""L3 screen reads — the doing-lane writer reads what his Mac's screen said, when it needs to.

The writer is given the day's cheap facts (ADR 0302); when they do not show what a stretch or a
key served, it may call :data:`SCREEN_TOOL` for the screen text of one key, or of every window,
between two of the times it was given (ADR 0395). The program answers with the windows
:mod:`jarvis.state.screen_text` reads, each line once per window with the time it was first seen,
cut into pages of at most :data:`PAGE_CHARS` characters at line ends; a call names the page it
wants. Times and keys are named by the labels the writer was given, so a call can only read the
day it writes. The runtime owns the reading and the loop.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.decision.llm import ChatResult
    from jarvis.state.screen_text import Window

SCREEN_TOOL_NAME: Final = "read_screen"
PAGE_CHARS: Final = 12_000
"""The most characters of screen text one answer holds; the rest is on the pages after it."""
MAX_ROUNDS: Final = 8
"""The most rounds of reads one answer may take before it is asked to answer."""
MAX_READ_CHARS: Final = 120_000
"""The most characters of screen text one answer may read, over all its rounds; past them a read
is told the screen is read enough."""
SPENT: Final = "the screen reads for this answer are spent; write it from what you have read"
SCREEN_TOOL: Final[dict[str, Any]] = {
    "name": SCREEN_TOOL_NAME,
    "description": (
        "Read the text on his Mac's screen between two of the listed times: for one key of NEW "
        "KEYS or VARYING KEYS, or for every window when no key is given. Each window comes with "
        "its app and title, and each of its lines once, after the time it was first seen. Long "
        "reads come in pages; the answer says how many there are."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "from": {"type": "string", "description": "a listed time, t0, t1, ..."},
            "to": {"type": "string", "description": "a later listed time"},
            "key": {"type": "string", "description": "k1, ... or v1, ...; leave out for all"},
            "page": {"type": "integer", "minimum": 1, "description": "1 when left out"},
        },
        "required": ["from", "to"],
    },
}

type Reader = Callable[[int, int, str | None], Sequence[Window]]
"""Reads the windows of ``[start_ms, end_ms)``, of one key or of all (None)."""


def requested(result: ChatResult) -> list[tuple[str, dict[str, Any]]]:
    """Each screen read the answer asks for, as (call id, its arguments); a bad one as ``{}``."""
    calls: list[tuple[str, dict[str, Any]]] = []
    for call in result.tool_calls:
        try:
            raw = json.loads(call.arguments_json or "{}")
        except ValueError:
            raw = {}
        calls.append((call.call_id, raw if isinstance(raw, dict) else {}))
    return calls


def _clock(ms: int, zone: tzinfo, *, seconds: bool = False) -> str:
    moment = datetime.fromtimestamp(ms / 1000, zone)
    return f"{moment:%H:%M:%S}" if seconds else f"{moment:%H:%M}"


type Line = tuple[str, str]
"""A line of a read: its kind (``head``, ``at`` or ``text``) and what it shows."""


def lines(found: Sequence[Window], zone: tzinfo) -> list[Line]:
    """``found`` as the writer reads it: a head per window, a time before its newly seen lines."""
    out: list[Line] = []
    for window in found:
        title = f" | {window.title}" if window.title else ""
        out.append((
            "head",
            f"== {_clock(window.first_ms, zone)}-{_clock(window.last_ms, zone)} | "
            f"{window.app}{title}",
        ))
        at = None
        for first, text in window.lines:
            if first != at:
                out.append(("at", f"@{_clock(first, zone, seconds=True)}"))
                at = first
            out.append(("text", text))
    return out


def pages(read: Sequence[Line]) -> list[str]:
    """:func:`lines` cut into pages of about :data:`PAGE_CHARS` characters at line ends.

    A page that starts inside a window opens with its head, marked continued, and the time its
    next lines were first seen. A line longer than a page is a page of its own, cut nowhere.
    """
    out: list[list[str]] = [[]]
    size = 0
    head, at = "", ""
    for kind, line in read:
        if out[-1] and size + len(line) + 1 > PAGE_CHARS:
            out.append([])
            size = 0
            if kind != "head":
                carried = [f"{head} (continued)", *([at] if at and kind != "at" else [])]
                out[-1].extend(carried)
                size = sum(len(one) + 1 for one in carried)
        if kind == "head":
            head, at = line, ""
        elif kind == "at":
            at = line
        out[-1].append(line)
        size += len(line) + 1
    return ["\n".join(page) for page in out if page]


def answer(  # noqa: PLR0913 — the call, the labels it may name, the reader and the zone.
    arguments: Mapping[str, Any], times: Mapping[str, int], keys: Mapping[str, str],
    names: Mapping[str, str], read: Reader, zone: tzinfo,
) -> tuple[str, bool]:
    """What the program says to one screen read, and whether it is an error.

    ``times`` and ``keys`` are the labels the writer was given, by label; ``names`` the keys'
    names. A read that names a time or key not given, or runs backwards, is told why.
    """
    start, end = times.get(str(arguments.get("from"))), times.get(str(arguments.get("to")))
    if start is None or end is None or end <= start:
        return "from and to must be two listed times, the first earlier", True
    label = arguments.get("key")
    key = None if label in (None, "") else keys.get(str(label))
    if label not in (None, "") and key is None:
        return f"{label!r} is not a key of NEW KEYS or VARYING KEYS", True
    page = arguments.get("page", 1)
    page = page if isinstance(page, int) and not isinstance(page, bool) else 0
    try:
        found = read(start, end, key)
    except Exception as exc:  # noqa: BLE001 — a store that cannot be read is said, not raised
        return f"the screen could not be read ({type(exc).__name__})", True
    what = "every window" if key is None else f"{label} {names.get(key, key)}"
    head = f"SCREEN {_clock(start, zone)}-{_clock(end, zone)}, {what}"
    cut = pages(lines(found, zone))
    if not cut:
        return f"{head}: nothing was read off the screen then", False
    if not 1 <= page <= len(cut):
        return f"{head}: page must be from 1 to {len(cut)}", True
    return f"{head}, page {page} of {len(cut)}:\n{cut[page - 1]}", False


__all__ = [
    "MAX_READ_CHARS",
    "MAX_ROUNDS",
    "PAGE_CHARS",
    "SCREEN_TOOL",
    "SCREEN_TOOL_NAME",
    "SPENT",
    "Line",
    "Reader",
    "answer",
    "lines",
    "pages",
    "requested",
]
