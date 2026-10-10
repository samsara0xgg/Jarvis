"""The item a paired phone has open when its owner speaks (ADR 0214).

``about`` rides on the opening row of a phone's turn: ``{"kind", "id", "title", "start_ms"}``.
It is the phone's claim, and its title can be a third party's words (an invite's subject), so
every reader goes through :func:`clean_about`: the closed key set, the id shape, and a title
flattened to one line. :func:`prompt_title` is the shorter form a prompt may carry.

Layer rules: stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final

ABOUT_KINDS: Final = (
    "reminder", "event", "todo", "stay", "move", "sleep", "work", "call", "talk",
)
"""What the phone may have open: a reminder, an Outlook event, a task, or a day-line item."""
MAX_ID_CHARS: Final = 256
MAX_TITLE_CHARS: Final = 200
MAX_START_MS: Final = 4_102_444_800_000
"""2100-01-01 UTC in epoch ms; the phone's times are well inside it."""
PROMPT_TITLE_CHARS: Final = 80

_ID: Final = re.compile(r"[A-Za-z0-9_\-=+/.|:]{1,256}")
_KEYS: Final = frozenset({"kind", "id", "title", "start_ms"})


def _one_line(text: str) -> str:
    """``text`` on one line: whitespace runs become one space, other control characters go."""
    spaced = "".join(" " if ch.isspace() else ch for ch in text)
    kept = "".join(ch for ch in spaced if unicodedata.category(ch) != "Cc")
    return " ".join(kept.split())


def clean_about(raw: object) -> dict[str, Any]:
    """The checked ``about`` of a phone's say or submit, or ``ValueError`` saying what is wrong.

    Keys are closed: ``kind`` and ``id`` are required, ``title`` and ``start_ms`` optional, and
    anything else is refused. The id is refused when it is too long or has a character outside
    the id set; it is never cut. The title is flattened to one line and clipped
    to :data:`MAX_TITLE_CHARS`. ``start_ms`` is an integer epoch ms in ``[0, MAX_START_MS]``.
    """
    if not isinstance(raw, dict) or not set(raw) <= _KEYS:
        msg = "about is an object with kind, id, title and start_ms"
        raise ValueError(msg)
    kind, about_id = raw.get("kind"), raw.get("id")
    if kind not in ABOUT_KINDS:
        msg = f"about.kind is one of {', '.join(ABOUT_KINDS)}"
        raise ValueError(msg)
    if not isinstance(about_id, str) or _ID.fullmatch(about_id) is None:
        msg = f"about.id is 1-{MAX_ID_CHARS} characters of [A-Za-z0-9_-=+/.|:]"
        raise ValueError(msg)
    cleaned: dict[str, Any] = {"kind": kind, "id": about_id}
    if "title" in raw:
        title = raw["title"]
        if not isinstance(title, str):
            msg = "about.title is text"
            raise ValueError(msg)
        cleaned["title"] = _one_line(title)[:MAX_TITLE_CHARS].rstrip()
    if "start_ms" in raw:
        start = raw["start_ms"]
        if type(start) is not int or not 0 <= start <= MAX_START_MS:
            msg = f"about.start_ms is an integer epoch ms in [0, {MAX_START_MS}]"
            raise ValueError(msg)
        cleaned["start_ms"] = start
    return cleaned


def prompt_title(title: str) -> str:
    """A title for one line of a prompt: one line, no double quotes, at most 80 characters."""
    flat = _one_line(title).replace('"', "'")
    if len(flat) <= PROMPT_TITLE_CHARS:
        return flat
    return flat[: PROMPT_TITLE_CHARS - 1].rstrip() + "…"
